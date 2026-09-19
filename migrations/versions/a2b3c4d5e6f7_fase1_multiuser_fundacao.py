"""fase 1 multiuser: fundacao persistente organizacional e configuracao

Revision ID: a2b3c4d5e6f7
Revises: z0a1b2c3d4e5
Create Date: 2026-09-09

PRECONDIÇÃO BLOQUEANTE DE DEPLOY:
A migration da Fase 1 NÃO pode ser promovida isoladamente para produção
enquanto o enforcement/reconciliação da Fase 2 não estiver no mesmo
conjunto seguro de release. O start.sh aplica `db upgrade` no boot;
promover esta revisão sozinha reabriria a janela operacional do fluxo
admin legado (Multiuser sem multiuser_ativa/quantity/vínculo).

Ordem segura:
1. colunas nullable / default compatível em conta
2. tabelas de vínculo e inconsistência de backfill
3. seed da quantidade mínima administrativa
4. backfill determinístico de legado Multiuser
5. índices/constraints parciais após o backfill

Não altera User.conta_id / User.franquia_id.
Não chama Stripe.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import text


revision = "a2b3c4d5e6f7"
down_revision = "z0a1b2c3d4e5"
branch_labels = None
depends_on = None

CHAVE_QTD_MINIMA = "plano_quantidade_minima_admin_multiuser"
DESC_QTD_MINIMA = "Quantidade mínima de assentos do plano Multiuser"


def upgrade():
    with op.batch_alter_table("conta", schema=None) as batch_op:
        batch_op.add_column(sa.Column("razao_social", sa.String(length=255), nullable=True))
        batch_op.add_column(sa.Column("nome_fantasia", sa.String(length=255), nullable=True))
        batch_op.add_column(sa.Column("cnpj", sa.String(length=14), nullable=True))
        batch_op.add_column(sa.Column("email_empresarial", sa.String(length=150), nullable=True))
        batch_op.add_column(sa.Column("endereco_logradouro", sa.String(length=255), nullable=True))
        batch_op.add_column(sa.Column("endereco_numero", sa.String(length=20), nullable=True))
        batch_op.add_column(sa.Column("endereco_complemento", sa.String(length=120), nullable=True))
        batch_op.add_column(sa.Column("endereco_bairro", sa.String(length=120), nullable=True))
        batch_op.add_column(sa.Column("endereco_cidade", sa.String(length=120), nullable=True))
        batch_op.add_column(sa.Column("endereco_uf", sa.String(length=2), nullable=True))
        batch_op.add_column(sa.Column("endereco_cep", sa.String(length=8), nullable=True))
        batch_op.add_column(sa.Column("quantidade_assentos_contratados", sa.Integer(), nullable=True))
        batch_op.add_column(
            sa.Column(
                "multiuser_ativa",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )
        batch_op.create_check_constraint(
            "ck_conta_qtd_assentos_positiva",
            "quantidade_assentos_contratados IS NULL OR quantidade_assentos_contratados >= 1",
        )

    op.create_index("ix_conta_multiuser_ativa", "conta", ["multiuser_ativa"], unique=False)

    op.create_table(
        "conta_vinculo_organizacional",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("conta_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("franquia_id", sa.Integer(), nullable=False),
        sa.Column("papel", sa.String(length=20), nullable=False),
        sa.Column("estado", sa.String(length=20), nullable=False),
        sa.Column("titular", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("origem", sa.String(length=40), nullable=False),
        sa.Column("criado_por_user_id", sa.Integer(), nullable=True),
        sa.Column("iniciado_em", sa.DateTime(), nullable=False),
        sa.Column("encerrado_em", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("papel IN ('contratante', 'membro')", name="ck_conta_vinculo_org_papel"),
        sa.CheckConstraint("estado IN ('ativo', 'encerrado')", name="ck_conta_vinculo_org_estado"),
        sa.CheckConstraint(
            "((papel = 'contratante' AND titular) OR (papel = 'membro' AND NOT titular))",
            name="ck_conta_vinculo_org_papel_titular",
        ),
        sa.CheckConstraint(
            "("
            "(estado = 'ativo' AND encerrado_em IS NULL) "
            "OR (estado = 'encerrado' AND encerrado_em IS NOT NULL)"
            ")",
            name="ck_conta_vinculo_org_encerramento",
        ),
        sa.ForeignKeyConstraint(["conta_id"], ["conta.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["user.id"]),
        sa.ForeignKeyConstraint(["franquia_id"], ["franquia.id"]),
        sa.ForeignKeyConstraint(["criado_por_user_id"], ["user.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_conta_vinculo_organizacional_conta_id",
        "conta_vinculo_organizacional",
        ["conta_id"],
        unique=False,
    )
    op.create_index(
        "ix_conta_vinculo_organizacional_user_id",
        "conta_vinculo_organizacional",
        ["user_id"],
        unique=False,
    )
    op.create_index(
        "ix_conta_vinculo_organizacional_franquia_id",
        "conta_vinculo_organizacional",
        ["franquia_id"],
        unique=False,
    )
    op.create_index(
        "ix_conta_vinculo_organizacional_papel",
        "conta_vinculo_organizacional",
        ["papel"],
        unique=False,
    )
    op.create_index(
        "ix_conta_vinculo_organizacional_estado",
        "conta_vinculo_organizacional",
        ["estado"],
        unique=False,
    )
    op.create_index(
        "ix_conta_vinculo_organizacional_criado_por_user_id",
        "conta_vinculo_organizacional",
        ["criado_por_user_id"],
        unique=False,
    )
    op.create_index(
        "ix_conta_vinculo_org_conta_estado",
        "conta_vinculo_organizacional",
        ["conta_id", "estado"],
        unique=False,
    )

    op.create_table(
        "conta_organizacional_backfill_inconsistencia",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("conta_id", sa.Integer(), nullable=True),
        sa.Column("franquia_id", sa.Integer(), nullable=True),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column("codigo", sa.String(length=80), nullable=False),
        sa.Column("detalhe", sa.String(length=255), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_conta_org_backfill_inconsistencia_conta_id",
        "conta_organizacional_backfill_inconsistencia",
        ["conta_id"],
        unique=False,
    )
    op.create_index(
        "ix_conta_org_backfill_inconsistencia_franquia_id",
        "conta_organizacional_backfill_inconsistencia",
        ["franquia_id"],
        unique=False,
    )
    op.create_index(
        "ix_conta_org_backfill_inconsistencia_user_id",
        "conta_organizacional_backfill_inconsistencia",
        ["user_id"],
        unique=False,
    )
    op.create_index(
        "ix_conta_org_backfill_inconsistencia_codigo",
        "conta_organizacional_backfill_inconsistencia",
        ["codigo"],
        unique=False,
    )

    conn = op.get_bind()
    conn.execute(
        text(
            """
            INSERT INTO config_regras (chave, valor_texto, valor_inteiro, valor_real, descricao)
            SELECT :chave, :valor_texto, :valor_inteiro, NULL, :descricao
             WHERE NOT EXISTS (
                SELECT 1 FROM config_regras WHERE chave = :chave
             )
            """
        ),
        {
            "chave": CHAVE_QTD_MINIMA,
            "valor_texto": "5",
            "valor_inteiro": 5,
            "descricao": DESC_QTD_MINIMA,
        },
    )

    from app.services.conta_organizacional_backfill_service import (
        aplicar_backfill_multiuser_legado,
    )

    aplicar_backfill_multiuser_legado(conn)

    op.create_index(
        "uq_conta_cnpj_multiuser_ativa",
        "conta",
        ["cnpj"],
        unique=True,
        postgresql_where=sa.text(
            "cnpj IS NOT NULL AND multiuser_ativa AND status = 'ativa'"
        ),
        sqlite_where=sa.text(
            "cnpj IS NOT NULL AND multiuser_ativa AND status = 'ativa'"
        ),
    )
    op.create_index(
        "uq_conta_vinculo_org_user_ativo",
        "conta_vinculo_organizacional",
        ["user_id"],
        unique=True,
        postgresql_where=sa.text("estado = 'ativo'"),
        sqlite_where=sa.text("estado = 'ativo'"),
    )
    op.create_index(
        "uq_conta_vinculo_org_franquia_ativo",
        "conta_vinculo_organizacional",
        ["franquia_id"],
        unique=True,
        postgresql_where=sa.text("estado = 'ativo'"),
        sqlite_where=sa.text("estado = 'ativo'"),
    )
    op.create_index(
        "uq_conta_vinculo_org_contratante_ativo",
        "conta_vinculo_organizacional",
        ["conta_id"],
        unique=True,
        postgresql_where=sa.text("estado = 'ativo' AND papel = 'contratante'"),
        sqlite_where=sa.text("estado = 'ativo' AND papel = 'contratante'"),
    )


def downgrade():
    op.drop_index(
        "uq_conta_vinculo_org_contratante_ativo",
        table_name="conta_vinculo_organizacional",
    )
    op.drop_index(
        "uq_conta_vinculo_org_franquia_ativo",
        table_name="conta_vinculo_organizacional",
    )
    op.drop_index(
        "uq_conta_vinculo_org_user_ativo",
        table_name="conta_vinculo_organizacional",
    )
    op.drop_index("uq_conta_cnpj_multiuser_ativa", table_name="conta")

    op.drop_index(
        "ix_conta_org_backfill_inconsistencia_codigo",
        table_name="conta_organizacional_backfill_inconsistencia",
    )
    op.drop_index(
        "ix_conta_org_backfill_inconsistencia_user_id",
        table_name="conta_organizacional_backfill_inconsistencia",
    )
    op.drop_index(
        "ix_conta_org_backfill_inconsistencia_franquia_id",
        table_name="conta_organizacional_backfill_inconsistencia",
    )
    op.drop_index(
        "ix_conta_org_backfill_inconsistencia_conta_id",
        table_name="conta_organizacional_backfill_inconsistencia",
    )
    op.drop_table("conta_organizacional_backfill_inconsistencia")

    op.drop_index(
        "ix_conta_vinculo_org_conta_estado",
        table_name="conta_vinculo_organizacional",
    )
    op.drop_index(
        "ix_conta_vinculo_organizacional_criado_por_user_id",
        table_name="conta_vinculo_organizacional",
    )
    op.drop_index(
        "ix_conta_vinculo_organizacional_estado",
        table_name="conta_vinculo_organizacional",
    )
    op.drop_index(
        "ix_conta_vinculo_organizacional_papel",
        table_name="conta_vinculo_organizacional",
    )
    op.drop_index(
        "ix_conta_vinculo_organizacional_franquia_id",
        table_name="conta_vinculo_organizacional",
    )
    op.drop_index(
        "ix_conta_vinculo_organizacional_user_id",
        table_name="conta_vinculo_organizacional",
    )
    op.drop_index(
        "ix_conta_vinculo_organizacional_conta_id",
        table_name="conta_vinculo_organizacional",
    )
    op.drop_table("conta_vinculo_organizacional")

    op.drop_index("ix_conta_multiuser_ativa", table_name="conta")
    with op.batch_alter_table("conta", schema=None) as batch_op:
        batch_op.drop_constraint("ck_conta_qtd_assentos_positiva", type_="check")
        batch_op.drop_column("multiuser_ativa")
        batch_op.drop_column("quantidade_assentos_contratados")
        batch_op.drop_column("endereco_cep")
        batch_op.drop_column("endereco_uf")
        batch_op.drop_column("endereco_cidade")
        batch_op.drop_column("endereco_bairro")
        batch_op.drop_column("endereco_complemento")
        batch_op.drop_column("endereco_numero")
        batch_op.drop_column("endereco_logradouro")
        batch_op.drop_column("email_empresarial")
        batch_op.drop_column("cnpj")
        batch_op.drop_column("nome_fantasia")
        batch_op.drop_column("razao_social")
