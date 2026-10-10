"""reserva atomica e liquidacao idempotente de chamada de IA

Revision ID: h6i7j8k9l0m1
Revises: g5h6i7j8k9l0
Create Date: 2026-10-08

Reserva pendente na franquia nao e consumo liquidado.
A tentativa fisica e o abatimento por evento garantem idempotencia.

Eventos ja existentes neste upgrade sao legado: o consumo_acumulado
anterior nao e recalculado e a marca impede debito novo.
"""
from alembic import op
import sqlalchemy as sa


revision = "h6i7j8k9l0m1"
down_revision = "g5h6i7j8k9l0"
branch_labels = None
depends_on = None

_STATUS_TENTATIVA = (
    "reserved",
    "calling",
    "settled",
    "blocked",
    "governance_blocked",
    "provider_failed",
    "uncertain",
    "released",
)


def upgrade():
    with op.batch_alter_table("franquia", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "reserva_pendente",
                sa.Numeric(18, 6),
                nullable=False,
                server_default="0",
            )
        )
        batch_op.create_check_constraint(
            "ck_franquia_reserva_pendente_nao_negativa",
            "reserva_pendente >= 0",
        )
    with op.batch_alter_table("ia_consumo_evento", schema=None) as batch_op:
        batch_op.add_column(sa.Column("regime_abatimento", sa.String(length=20), nullable=True))
    op.execute(
        "UPDATE ia_consumo_evento SET regime_abatimento = 'legado' "
        "WHERE regime_abatimento IS NULL"
    )
    op.create_index(
        "ix_ia_consumo_evento_regime_abatimento",
        "ia_consumo_evento",
        ["regime_abatimento"],
    )
    op.create_table(
        "ia_consumo_abatimento",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("ia_consumo_evento_id", sa.Integer(), nullable=False),
        sa.Column("franquia_id", sa.Integer(), nullable=True),
        sa.Column("creditos", sa.Numeric(18, 6), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("creditos >= 0", name="ck_ia_consumo_abatimento_creditos_nao_negativo"),
        sa.ForeignKeyConstraint(
            ["franquia_id"],
            ["franquia.id"],
            name="fk_ia_consumo_abatimento_franquia",
        ),
        sa.ForeignKeyConstraint(
            ["ia_consumo_evento_id"],
            ["ia_consumo_evento.id"],
            name="fk_ia_consumo_abatimento_evento",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("ia_consumo_evento_id", name="uq_ia_consumo_abatimento_evento"),
    )
    op.create_index(
        "ix_ia_consumo_abatimento_franquia_id",
        "ia_consumo_abatimento",
        ["franquia_id"],
    )
    op.execute(
        """
        INSERT INTO ia_consumo_abatimento (ia_consumo_evento_id, franquia_id, creditos, created_at)
        SELECT e.id,
               (SELECT f.id FROM franquia f WHERE f.id = e.franquia_id),
               0,
               CURRENT_TIMESTAMP
        FROM ia_consumo_evento e
        WHERE NOT EXISTS (
            SELECT 1 FROM ia_consumo_abatimento a WHERE a.ia_consumo_evento_id = e.id
        )
        """
    )
    op.create_table(
        "ia_chamada_tentativa",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("attempt_key", sa.String(length=160), nullable=False),
        sa.Column("conta_id", sa.Integer(), nullable=True),
        sa.Column("franquia_id", sa.Integer(), nullable=True),
        sa.Column("usuario_id", sa.Integer(), nullable=True),
        sa.Column("origem_sistema", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("agent", sa.String(length=80), nullable=False),
        sa.Column("flow_type", sa.String(length=80), nullable=False),
        sa.Column("operation", sa.String(length=40), nullable=False),
        sa.Column("provider", sa.String(length=40), nullable=False),
        sa.Column("model", sa.String(length=255), nullable=False),
        sa.Column("ciclo_inicio", sa.DateTime(), nullable=True),
        sa.Column("ciclo_fim", sa.DateTime(), nullable=True),
        sa.Column("reserved_credits", sa.Numeric(18, 6), nullable=False, server_default="0"),
        sa.Column("actual_credits", sa.Numeric(18, 6), nullable=True),
        sa.Column("status", sa.String(length=40), nullable=False),
        sa.Column("ia_consumo_evento_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("settled_at", sa.DateTime(), nullable=True),
        sa.Column("failure_reason", sa.String(length=200), nullable=True),
        sa.Column("reserva_ativa", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("debita_cliente", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.CheckConstraint(
            "reserved_credits >= 0",
            name="ck_ia_chamada_tentativa_reserva_nao_negativa",
        ),
        sa.CheckConstraint(
            "actual_credits IS NULL OR actual_credits >= 0",
            name="ck_ia_chamada_tentativa_credito_real_nao_negativo",
        ),
        sa.CheckConstraint(
            "status IN ({})".format(", ".join(f"'{status}'" for status in _STATUS_TENTATIVA)),
            name="ck_ia_chamada_tentativa_status",
        ),
        sa.ForeignKeyConstraint(["conta_id"], ["conta.id"], name="fk_ia_chamada_tentativa_conta"),
        sa.ForeignKeyConstraint(
            ["franquia_id"],
            ["franquia.id"],
            name="fk_ia_chamada_tentativa_franquia",
        ),
        sa.ForeignKeyConstraint(
            ["ia_consumo_evento_id"],
            ["ia_consumo_evento.id"],
            name="fk_ia_chamada_tentativa_evento",
        ),
        sa.ForeignKeyConstraint(["usuario_id"], ["user.id"], name="fk_ia_chamada_tentativa_usuario"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("attempt_key"),
        sa.UniqueConstraint("ia_consumo_evento_id"),
    )
    op.create_index("ix_ia_chamada_tentativa_attempt_key", "ia_chamada_tentativa", ["attempt_key"])
    op.create_index("ix_ia_chamada_tentativa_conta_id", "ia_chamada_tentativa", ["conta_id"])
    op.create_index("ix_ia_chamada_tentativa_franquia_id", "ia_chamada_tentativa", ["franquia_id"])
    op.create_index("ix_ia_chamada_tentativa_usuario_id", "ia_chamada_tentativa", ["usuario_id"])
    op.create_index("ix_ia_chamada_tentativa_origem_sistema", "ia_chamada_tentativa", ["origem_sistema"])
    op.create_index("ix_ia_chamada_tentativa_agent", "ia_chamada_tentativa", ["agent"])
    op.create_index("ix_ia_chamada_tentativa_flow_type", "ia_chamada_tentativa", ["flow_type"])
    op.create_index("ix_ia_chamada_tentativa_status", "ia_chamada_tentativa", ["status"])
    op.create_index("ix_ia_chamada_tentativa_evento", "ia_chamada_tentativa", ["ia_consumo_evento_id"])
    op.create_index("ix_ia_chamada_tentativa_created_at", "ia_chamada_tentativa", ["created_at"])


def downgrade():
    op.drop_index("ix_ia_chamada_tentativa_created_at", table_name="ia_chamada_tentativa")
    op.drop_index("ix_ia_chamada_tentativa_evento", table_name="ia_chamada_tentativa")
    op.drop_index("ix_ia_chamada_tentativa_status", table_name="ia_chamada_tentativa")
    op.drop_index("ix_ia_chamada_tentativa_flow_type", table_name="ia_chamada_tentativa")
    op.drop_index("ix_ia_chamada_tentativa_agent", table_name="ia_chamada_tentativa")
    op.drop_index("ix_ia_chamada_tentativa_origem_sistema", table_name="ia_chamada_tentativa")
    op.drop_index("ix_ia_chamada_tentativa_usuario_id", table_name="ia_chamada_tentativa")
    op.drop_index("ix_ia_chamada_tentativa_franquia_id", table_name="ia_chamada_tentativa")
    op.drop_index("ix_ia_chamada_tentativa_conta_id", table_name="ia_chamada_tentativa")
    op.drop_index("ix_ia_chamada_tentativa_attempt_key", table_name="ia_chamada_tentativa")
    op.drop_table("ia_chamada_tentativa")
    op.drop_index("ix_ia_consumo_abatimento_franquia_id", table_name="ia_consumo_abatimento")
    op.drop_table("ia_consumo_abatimento")
    op.drop_index("ix_ia_consumo_evento_regime_abatimento", table_name="ia_consumo_evento")
    with op.batch_alter_table("ia_consumo_evento", schema=None) as batch_op:
        batch_op.drop_column("regime_abatimento")
    with op.batch_alter_table("franquia", schema=None) as batch_op:
        batch_op.drop_constraint("ck_franquia_reserva_pendente_nao_negativa", type_="check")
        batch_op.drop_column("reserva_pendente")
