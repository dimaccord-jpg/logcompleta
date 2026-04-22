"""fase 1: reforco estrutural de idempotencia e vinculo ativo

Revision ID: p9q0r1s2t3u4
Revises: o8p9q0r1s2t3
Create Date: 2026-04-16
"""
from alembic import op
import sqlalchemy as sa


revision = "p9q0r1s2t3u4"
down_revision = "o8p9q0r1s2t3"
branch_labels = None
depends_on = None


def upgrade():
    # Garante no maximo um vinculo ativo por conta, preservando historico.
    op.execute(
        sa.text(
            """
            WITH ranked AS (
              SELECT id, conta_id,
                     ROW_NUMBER() OVER (PARTITION BY conta_id ORDER BY id DESC) AS rn
              FROM conta_monetizacao_vinculo
              WHERE ativo IS TRUE
            )
            UPDATE conta_monetizacao_vinculo v
               SET ativo = FALSE,
                   desativado_em = COALESCE(v.desativado_em, CURRENT_TIMESTAMP)
              FROM ranked r
             WHERE v.id = r.id
               AND r.rn > 1
            """
        )
    )

    # Garante idempotencia estrutural sem remover fatos historicos: duplicatas perdem a chave.
    op.execute(
        sa.text(
            """
            WITH ranked AS (
              SELECT id,
                     ROW_NUMBER() OVER (PARTITION BY idempotency_key ORDER BY id ASC) AS rn
              FROM monetizacao_fato
              WHERE idempotency_key IS NOT NULL
            )
            UPDATE monetizacao_fato f
               SET idempotency_key = NULL
              FROM ranked r
             WHERE f.id = r.id
               AND r.rn > 1
            """
        )
    )

    op.create_index(
        "uq_conta_monetizacao_vinculo_conta_ativo_true",
        "conta_monetizacao_vinculo",
        ["conta_id"],
        unique=True,
        postgresql_where=sa.text("ativo IS TRUE"),
    )
    op.create_index(
        "uq_monetizacao_fato_idempotency_key_not_null",
        "monetizacao_fato",
        ["idempotency_key"],
        unique=True,
        postgresql_where=sa.text("idempotency_key IS NOT NULL"),
    )


def downgrade():
    op.drop_index(
        "uq_monetizacao_fato_idempotency_key_not_null",
        table_name="monetizacao_fato",
    )
    op.drop_index(
        "uq_conta_monetizacao_vinculo_conta_ativo_true",
        table_name="conta_monetizacao_vinculo",
    )
