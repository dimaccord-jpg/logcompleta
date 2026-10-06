"""régua: interações WhatsApp por crédito e estados de apropriação

Revision ID: b9c0d1e2f3g4
Revises: a8b9c0d1e2f3
Create Date: 2026-10-03

A taxa nasce nula. Não há default comercial.
Consumo já pendente continua válido. Apropriação passa a gravar créditos.

Manter os predicados iguais aos de ConsumoInteracaoCanal.
"""
from alembic import op
import sqlalchemy as sa


revision = "b9c0d1e2f3g4"
down_revision = "a8b9c0d1e2f3"
branch_labels = None
depends_on = None

_SQL_ESTADO = (
    "estado IN ("
    "'pronta_para_apropriacao', 'apropriada', 'erro_apropriacao', 'falha_tecnica'"
    ")"
)
_SQL_MOTIVO = (
    "motivo IN ("
    "'taxa_comercial_whatsapp_pendente', 'apropriada', 'erro_apropriacao', "
    "'falha_registro', 'contexto_indisponivel'"
    ")"
)
_SQL_CREDITOS = (
    "("
    "(estado = 'apropriada' AND creditos_apropriados IS NOT NULL "
    "AND creditos_apropriados > 0)"
    " OR (estado <> 'apropriada' AND creditos_apropriados IS NULL)"
    ")"
)
_SQL_COERENCIA = (
    "("
    "(estado = 'pronta_para_apropriacao' "
    "AND motivo = 'taxa_comercial_whatsapp_pendente' "
    "AND creditos_apropriados IS NULL "
    "AND user_id IS NOT NULL AND conta_id IS NOT NULL AND franquia_id IS NOT NULL)"
    " OR (estado = 'apropriada' "
    "AND motivo = 'apropriada' "
    "AND creditos_apropriados IS NOT NULL AND creditos_apropriados > 0 "
    "AND user_id IS NOT NULL AND conta_id IS NOT NULL AND franquia_id IS NOT NULL)"
    " OR (estado = 'erro_apropriacao' "
    "AND motivo = 'erro_apropriacao' "
    "AND creditos_apropriados IS NULL "
    "AND user_id IS NOT NULL AND conta_id IS NOT NULL AND franquia_id IS NOT NULL)"
    " OR (estado = 'falha_tecnica' AND user_id IS NOT NULL "
    "AND creditos_apropriados IS NULL "
    "AND motivo IN ('falha_registro', 'contexto_indisponivel'))"
    ")"
)
_SQL_ESTADO_3G1 = "estado IN ('pronta_para_apropriacao', 'falha_tecnica')"
_SQL_MOTIVO_3G1 = (
    "motivo IN ("
    "'taxa_comercial_whatsapp_pendente', 'falha_registro', 'contexto_indisponivel'"
    ")"
)
_SQL_CREDITOS_3G1 = "creditos_apropriados IS NULL"
_SQL_COERENCIA_3G1 = (
    "("
    "(estado = 'pronta_para_apropriacao' "
    "AND motivo = 'taxa_comercial_whatsapp_pendente' "
    "AND user_id IS NOT NULL AND conta_id IS NOT NULL AND franquia_id IS NOT NULL)"
    " OR (estado = 'falha_tecnica' AND user_id IS NOT NULL "
    "AND motivo IN ('falha_registro', 'contexto_indisponivel'))"
    ")"
)


def upgrade():
    with op.batch_alter_table("cleiton_cost_config", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("interacoes_whatsapp_por_credito", sa.Float(), nullable=True)
        )
    with op.batch_alter_table("consumo_interacao_canal", schema=None) as batch_op:
        batch_op.drop_constraint("ck_consumo_interacao_canal_estado", type_="check")
        batch_op.drop_constraint("ck_consumo_interacao_canal_motivo", type_="check")
        batch_op.drop_constraint("ck_consumo_interacao_canal_creditos", type_="check")
        batch_op.drop_constraint("ck_consumo_interacao_canal_coerencia", type_="check")
        batch_op.create_check_constraint("ck_consumo_interacao_canal_estado", _SQL_ESTADO)
        batch_op.create_check_constraint("ck_consumo_interacao_canal_motivo", _SQL_MOTIVO)
        batch_op.create_check_constraint(
            "ck_consumo_interacao_canal_creditos", _SQL_CREDITOS
        )
        batch_op.create_check_constraint(
            "ck_consumo_interacao_canal_coerencia", _SQL_COERENCIA
        )


def downgrade():
    with op.batch_alter_table("consumo_interacao_canal", schema=None) as batch_op:
        batch_op.drop_constraint("ck_consumo_interacao_canal_estado", type_="check")
        batch_op.drop_constraint("ck_consumo_interacao_canal_motivo", type_="check")
        batch_op.drop_constraint("ck_consumo_interacao_canal_creditos", type_="check")
        batch_op.drop_constraint("ck_consumo_interacao_canal_coerencia", type_="check")
        batch_op.create_check_constraint(
            "ck_consumo_interacao_canal_estado", _SQL_ESTADO_3G1
        )
        batch_op.create_check_constraint(
            "ck_consumo_interacao_canal_motivo", _SQL_MOTIVO_3G1
        )
        batch_op.create_check_constraint(
            "ck_consumo_interacao_canal_creditos", _SQL_CREDITOS_3G1
        )
        batch_op.create_check_constraint(
            "ck_consumo_interacao_canal_coerencia", _SQL_COERENCIA_3G1
        )
    with op.batch_alter_table("cleiton_cost_config", schema=None) as batch_op:
        batch_op.drop_column("interacoes_whatsapp_por_credito")
