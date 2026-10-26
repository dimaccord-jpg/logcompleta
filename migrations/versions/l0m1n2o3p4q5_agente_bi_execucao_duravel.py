"""fila durável de execução do AgenteBI

Revision ID: l0m1n2o3p4q5
Revises: k9l0m1n2o3p4
Create Date: 2026-10-23

A rodada de análise não tem lease, fencing nem contador de recuperação.
Esses dados operacionais ficam numa tabela nova. O histórico da análise
e a trilha de IaChamadaTentativa não são reescritos. Não há coluna de
dataset, prompt ou resposta.

Não implantar o worker junto com esta migration.
"""
from alembic import op
import sqlalchemy as sa

from app.agente_bi_execucao_integridade import instalar_protecao_execucao


revision = "l0m1n2o3p4q5"
down_revision = "k9l0m1n2o3p4"
branch_labels = None
depends_on = None

_ESTADOS = (
    "'criada', 'aguardando', 'adquirida', 'concluida', "
    "'falha_recuperavel', 'falha_definitiva', 'incerta', 'cancelada'"
)
_EFEITOS = "'nenhum', 'simulado', 'incerto'"
_DESFECHOS = (
    "'ausente', 'reservado', 'liquidado', 'dispensado', 'incerto', 'dispensar_pendente'"
)


def upgrade():
    op.create_table(
        "agente_bi_execucao",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("operacao_id", sa.String(length=36), nullable=False),
        sa.Column("analise_id", sa.Integer(), nullable=False),
        sa.Column("conta_id", sa.Integer(), nullable=False),
        sa.Column("franquia_id", sa.Integer(), nullable=False),
        sa.Column("usuario_id", sa.Integer(), nullable=False),
        sa.Column("ia_chamada_tentativa_id", sa.Integer(), nullable=True),
        sa.Column("estado", sa.String(length=32), nullable=False),
        sa.Column("tentativas", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("recuperacoes", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("chave_idempotencia", sa.String(length=80), nullable=False),
        sa.Column("fencing", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("lease_worker", sa.String(length=64), nullable=True),
        sa.Column("lease_ate", sa.DateTime(), nullable=True),
        sa.Column("efeito_externo", sa.String(length=16), nullable=False, server_default="nenhum"),
        sa.Column("ultimo_erro", sa.String(length=40), nullable=True),
        sa.Column("codigo_resultado", sa.String(length=40), nullable=True),
        sa.Column("desfecho_financeiro", sa.String(length=24), nullable=True),
        sa.Column("financeiro_encerrado", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("adquirida_em", sa.DateTime(), nullable=True),
        sa.Column("concluida_em", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(f"estado IN ({_ESTADOS})", name="ck_agente_bi_execucao_estado"),
        sa.CheckConstraint(f"efeito_externo IN ({_EFEITOS})", name="ck_agente_bi_execucao_efeito"),
        sa.CheckConstraint(
            f"desfecho_financeiro IS NULL OR desfecho_financeiro IN ({_DESFECHOS})",
            name="ck_agente_bi_execucao_desfecho",
        ),
        sa.CheckConstraint("fencing >= 0", name="ck_agente_bi_execucao_fencing"),
        sa.CheckConstraint("tentativas >= 0", name="ck_agente_bi_execucao_tentativas"),
        sa.CheckConstraint("recuperacoes >= 0", name="ck_agente_bi_execucao_recuperacoes"),
        sa.ForeignKeyConstraint(
            ["analise_id", "usuario_id", "conta_id", "franquia_id"],
            [
                "agente_bi_analise.id",
                "agente_bi_analise.usuario_id",
                "agente_bi_analise.conta_id",
                "agente_bi_analise.franquia_id",
            ],
            name="fk_agente_bi_execucao_analise_escopo",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["ia_chamada_tentativa_id"],
            ["ia_chamada_tentativa.id"],
            name="fk_agente_bi_execucao_tentativa",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("operacao_id", name="uq_agente_bi_execucao_operacao"),
        sa.UniqueConstraint("analise_id", name="uq_agente_bi_execucao_analise"),
        sa.UniqueConstraint("chave_idempotencia", name="uq_agente_bi_execucao_idempotencia"),
    )
    op.create_index(
        "uq_agente_bi_execucao_tentativa",
        "agente_bi_execucao",
        ["ia_chamada_tentativa_id"],
        unique=True,
        sqlite_where=sa.text("ia_chamada_tentativa_id IS NOT NULL"),
        postgresql_where=sa.text("ia_chamada_tentativa_id IS NOT NULL"),
    )
    op.create_index(
        "ix_agente_bi_execucao_fila",
        "agente_bi_execucao",
        ["estado", "id"],
        unique=False,
    )
    op.create_index(
        "ix_agente_bi_execucao_escopo",
        "agente_bi_execucao",
        ["conta_id", "franquia_id", "usuario_id"],
        unique=False,
    )
    op.create_table(
        "agente_bi_execucao_evento",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("execucao_id", sa.Integer(), nullable=False),
        sa.Column("estado_anterior", sa.String(length=32), nullable=True),
        sa.Column("estado_novo", sa.String(length=32), nullable=False),
        sa.Column("fencing", sa.Integer(), nullable=False),
        sa.Column("codigo", sa.String(length=40), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(f"estado_novo IN ({_ESTADOS})", name="ck_agente_bi_execucao_evento_destino"),
        sa.CheckConstraint(
            f"estado_anterior IS NULL OR estado_anterior IN ({_ESTADOS})",
            name="ck_agente_bi_execucao_evento_origem",
        ),
        sa.CheckConstraint("fencing >= 0", name="ck_agente_bi_execucao_evento_fencing"),
        sa.ForeignKeyConstraint(
            ["execucao_id"],
            ["agente_bi_execucao.id"],
            name="fk_agente_bi_execucao_evento_execucao",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_agente_bi_execucao_evento_execucao",
        "agente_bi_execucao_evento",
        ["execucao_id"],
        unique=False,
    )
    instalar_protecao_execucao(op.get_bind())


def downgrade():
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        for nome, tabela in (
            ("trg_agente_bi_execucao_evento_imutavel", "agente_bi_execucao_evento"),
            ("trg_agente_bi_execucao_evento_preservar", "agente_bi_execucao_evento"),
            ("trg_agente_bi_execucao_identidade", "agente_bi_execucao"),
            ("trg_agente_bi_execucao_tentativa", "agente_bi_execucao"),
            ("trg_agente_bi_execucao_preservar", "agente_bi_execucao"),
        ):
            op.execute(sa.text(f"DROP TRIGGER IF EXISTS {nome} ON {tabela}"))
        op.execute(sa.text("DROP FUNCTION IF EXISTS agente_bi_execucao_proteger()"))
    op.drop_index("ix_agente_bi_execucao_evento_execucao", table_name="agente_bi_execucao_evento")
    op.drop_table("agente_bi_execucao_evento")
    op.drop_index("ix_agente_bi_execucao_escopo", table_name="agente_bi_execucao")
    op.drop_index("ix_agente_bi_execucao_fila", table_name="agente_bi_execucao")
    op.drop_index("uq_agente_bi_execucao_tentativa", table_name="agente_bi_execucao")
    op.drop_table("agente_bi_execucao")
