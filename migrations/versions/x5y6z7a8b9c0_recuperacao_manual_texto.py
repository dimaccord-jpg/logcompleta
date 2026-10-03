"""recuperação manual de texto comum no canal

Revision ID: x5y6z7a8b9c0
Revises: w4x5y6z7a8b9
Create Date: 2026-10-02

Libera a tentativa 2 e a 3, a origem recuperacao_manual e o motivo
fechado. A entrega passa a poder ser lida por tentativa. Não cria
tentativa nova para saídas já gravadas.

Manter os predicados iguais aos de TentativaEnvioCanal e
EstadoEntregaTentativaCanal.
"""
from alembic import op
import sqlalchemy as sa


revision = "x5y6z7a8b9c0"
down_revision = "w4x5y6z7a8b9"
branch_labels = None
depends_on = None

_SQL_NUMERO = "numero_tentativa >= 1 AND numero_tentativa <= 3"
_SQL_ORIGEM = "origem_tentativa IN ('envio_inicial', 'recuperacao_manual')"
_SQL_MOTIVO = (
    "("
    "(origem_tentativa = 'envio_inicial' AND motivo_recuperacao IS NULL)"
    " OR (origem_tentativa = 'recuperacao_manual' AND motivo_recuperacao IN ("
    "'configuracao_corrigida', 'falha_http', 'resultado_incerto', "
    "'falha_entrega_provider', 'operacao_manual'))"
    ")"
)
_SQL_NUMERO_ANTERIOR = "numero_tentativa = 1"
_SQL_ORIGEM_ANTERIOR = "origem_tentativa = 'envio_inicial'"

_SQL_ESTADO_VERSAO = "versao >= 0"
_SQL_ESTADO_STATUS = (
    "status_entrega IS NULL OR status_entrega IN ("
    "'sent', 'delivered', 'read', 'failed')"
)
_SQL_ESTADO_FALHA = (
    "codigo_falha_entrega IS NULL OR codigo_falha_entrega = 'falha_entrega'"
)
_SQL_ESTADO_CLASSIFICACAO = (
    "classificacao_resultado IS NULL "
    "OR classificacao_resultado = 'resultado_incerto'"
)
_SQL_ESTADO_FORMA = (
    "("
    "(classificacao_resultado = 'resultado_incerto' "
    "AND status_entrega IS NULL AND provider_status_em IS NULL "
    "AND sent_em IS NULL AND delivered_em IS NULL AND read_em IS NULL "
    "AND failed_em IS NULL AND codigo_falha_entrega IS NULL)"
    " OR (classificacao_resultado IS NULL AND provider_status_em IS NOT NULL "
    "AND ("
    "(status_entrega = 'sent' AND sent_em IS NOT NULL "
    "AND delivered_em IS NULL AND read_em IS NULL "
    "AND failed_em IS NULL AND codigo_falha_entrega IS NULL)"
    " OR (status_entrega = 'delivered' AND delivered_em IS NOT NULL "
    "AND read_em IS NULL AND ("
    "(failed_em IS NULL AND codigo_falha_entrega IS NULL)"
    " OR (failed_em IS NOT NULL AND codigo_falha_entrega = 'falha_entrega')"
    "))"
    " OR (status_entrega = 'read' AND read_em IS NOT NULL AND ("
    "(failed_em IS NULL AND codigo_falha_entrega IS NULL)"
    " OR (failed_em IS NOT NULL AND codigo_falha_entrega = 'falha_entrega')"
    "))"
    " OR (status_entrega = 'failed' AND failed_em IS NOT NULL "
    "AND codigo_falha_entrega = 'falha_entrega' "
    "AND delivered_em IS NULL AND read_em IS NULL)"
    "))"
    ")"
)

_BACKFILL_ENTREGA = """
INSERT INTO estado_entrega_tentativa_canal (
    tentativa_envio_id,
    saida_id,
    versao,
    status_entrega,
    provider_status_em,
    sent_em,
    delivered_em,
    read_em,
    failed_em,
    codigo_falha_entrega,
    classificacao_resultado
)
SELECT
    t.id,
    e.saida_id,
    e.versao,
    e.status_entrega,
    e.provider_status_em,
    e.sent_em,
    e.delivered_em,
    e.read_em,
    e.failed_em,
    e.codigo_falha_entrega,
    e.classificacao_resultado
FROM estado_entrega_canal_saida AS e
JOIN tentativa_envio_canal AS t
    ON t.saida_id = e.saida_id
   AND t.numero_tentativa = 1
WHERE NOT EXISTS (
    SELECT 1
    FROM tentativa_envio_canal AS posterior
    WHERE posterior.saida_id = e.saida_id
      AND posterior.numero_tentativa > 1
)
"""

_BACKFILL_APLICACAO = """
UPDATE aplicacao_status_canal
SET tentativa_envio_id = (
    SELECT t.id
    FROM tentativa_envio_canal AS t
    WHERE t.saida_id = aplicacao_status_canal.saida_id
      AND t.numero_tentativa = 1
)
WHERE saida_id IS NOT NULL
  AND tentativa_envio_id IS NULL
"""


def upgrade():
    with op.batch_alter_table("tentativa_envio_canal", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("motivo_recuperacao", sa.String(length=32), nullable=True)
        )
    with op.batch_alter_table("tentativa_envio_canal", schema=None) as batch_op:
        batch_op.drop_constraint("ck_tentativa_envio_canal_numero", type_="check")
        batch_op.drop_constraint("ck_tentativa_envio_canal_origem", type_="check")
        batch_op.create_check_constraint("ck_tentativa_envio_canal_numero", _SQL_NUMERO)
        batch_op.create_check_constraint("ck_tentativa_envio_canal_origem", _SQL_ORIGEM)
        batch_op.create_check_constraint("ck_tentativa_envio_canal_motivo", _SQL_MOTIVO)
    with op.batch_alter_table("aplicacao_status_canal", schema=None) as batch_op:
        batch_op.add_column(sa.Column("tentativa_envio_id", sa.Integer(), nullable=True))
        batch_op.create_foreign_key(
            "fk_aplicacao_status_canal_tentativa_envio",
            "tentativa_envio_canal",
            ["tentativa_envio_id"],
            ["id"],
        )
        batch_op.create_index(
            "ix_aplicacao_status_canal_tentativa_envio_id",
            ["tentativa_envio_id"],
        )
    op.execute(_BACKFILL_APLICACAO)
    op.create_table(
        "estado_entrega_tentativa_canal",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("tentativa_envio_id", sa.Integer(), nullable=False),
        sa.Column("saida_id", sa.Integer(), nullable=False),
        sa.Column("versao", sa.Integer(), nullable=False),
        sa.Column("status_entrega", sa.String(length=16), nullable=True),
        sa.Column("provider_status_em", sa.DateTime(), nullable=True),
        sa.Column("sent_em", sa.DateTime(), nullable=True),
        sa.Column("delivered_em", sa.DateTime(), nullable=True),
        sa.Column("read_em", sa.DateTime(), nullable=True),
        sa.Column("failed_em", sa.DateTime(), nullable=True),
        sa.Column("codigo_falha_entrega", sa.String(length=32), nullable=True),
        sa.Column("classificacao_resultado", sa.String(length=32), nullable=True),
        sa.CheckConstraint(_SQL_ESTADO_VERSAO, name="ck_estado_entrega_tentativa_versao"),
        sa.CheckConstraint(_SQL_ESTADO_STATUS, name="ck_estado_entrega_tentativa_status"),
        sa.CheckConstraint(_SQL_ESTADO_FALHA, name="ck_estado_entrega_tentativa_falha"),
        sa.CheckConstraint(
            _SQL_ESTADO_CLASSIFICACAO,
            name="ck_estado_entrega_tentativa_classificacao",
        ),
        sa.CheckConstraint(_SQL_ESTADO_FORMA, name="ck_estado_entrega_tentativa_forma"),
        sa.ForeignKeyConstraint(
            ["tentativa_envio_id"],
            ["tentativa_envio_canal.id"],
            name="fk_estado_entrega_tentativa_envio",
        ),
        sa.ForeignKeyConstraint(
            ["saida_id"],
            ["evento_canal_saida.id"],
            name="fk_estado_entrega_tentativa_saida",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tentativa_envio_id",
            name="uq_estado_entrega_tentativa_canal",
        ),
    )
    op.create_index(
        "ix_estado_entrega_tentativa_canal_saida_id",
        "estado_entrega_tentativa_canal",
        ["saida_id"],
    )
    op.execute(_BACKFILL_ENTREGA)


def downgrade():
    op.drop_index(
        "ix_estado_entrega_tentativa_canal_saida_id",
        table_name="estado_entrega_tentativa_canal",
    )
    op.drop_table("estado_entrega_tentativa_canal")
    with op.batch_alter_table("aplicacao_status_canal", schema=None) as batch_op:
        batch_op.drop_index("ix_aplicacao_status_canal_tentativa_envio_id")
        batch_op.drop_constraint(
            "fk_aplicacao_status_canal_tentativa_envio",
            type_="foreignkey",
        )
        batch_op.drop_column("tentativa_envio_id")
    op.execute(
        sa.text(
            "DELETE FROM tentativa_envio_canal WHERE numero_tentativa > 1"
        )
    )
    with op.batch_alter_table("tentativa_envio_canal", schema=None) as batch_op:
        batch_op.drop_constraint("ck_tentativa_envio_canal_motivo", type_="check")
        batch_op.drop_constraint("ck_tentativa_envio_canal_numero", type_="check")
        batch_op.drop_constraint("ck_tentativa_envio_canal_origem", type_="check")
        batch_op.drop_column("motivo_recuperacao")
        batch_op.create_check_constraint(
            "ck_tentativa_envio_canal_numero",
            _SQL_NUMERO_ANTERIOR,
        )
        batch_op.create_check_constraint(
            "ck_tentativa_envio_canal_origem",
            _SQL_ORIGEM_ANTERIOR,
        )
