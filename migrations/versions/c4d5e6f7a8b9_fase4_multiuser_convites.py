"""fase 4: convites multiuser persistentes

Revision ID: c4d5e6f7a8b9
Revises: b3c4d5e6f7a8
Create Date: 2026-09-10

Convite organizacional com reserva de Franquia, destinatário, expiração e aceite.
Não evolui MultiuserFranquiaCodigo (legado de código de acesso, sem destinatário/TTL/aceite).
Não transforma códigos legados em convites ativos.
"""
from alembic import op
import sqlalchemy as sa


revision = "c4d5e6f7a8b9"
down_revision = "b3c4d5e6f7a8"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "conta_multiuser_convite",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("conta_id", sa.Integer(), nullable=False),
        sa.Column("franquia_id", sa.Integer(), nullable=False),
        sa.Column("criado_por_user_id", sa.Integer(), nullable=False),
        sa.Column("email_destino", sa.String(length=150), nullable=False),
        sa.Column("estado", sa.String(length=20), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("enviado_em", sa.DateTime(), nullable=True),
        sa.Column("reenviado_em", sa.DateTime(), nullable=True),
        sa.Column("accepted_at", sa.DateTime(), nullable=True),
        sa.Column("accepted_user_id", sa.Integer(), nullable=True),
        sa.CheckConstraint(
            "estado IN ('pendente', 'aceito', 'expirado')",
            name="ck_conta_multiuser_convite_estado",
        ),
        sa.ForeignKeyConstraint(["conta_id"], ["conta.id"]),
        sa.ForeignKeyConstraint(["franquia_id"], ["franquia.id"]),
        sa.ForeignKeyConstraint(["criado_por_user_id"], ["user.id"]),
        sa.ForeignKeyConstraint(["accepted_user_id"], ["user.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_conta_multiuser_convite_conta_id",
        "conta_multiuser_convite",
        ["conta_id"],
        unique=False,
    )
    op.create_index(
        "ix_conta_multiuser_convite_franquia_id",
        "conta_multiuser_convite",
        ["franquia_id"],
        unique=False,
    )
    op.create_index(
        "ix_conta_multiuser_convite_estado",
        "conta_multiuser_convite",
        ["estado"],
        unique=False,
    )
    op.create_index(
        "ix_conta_multiuser_convite_email_destino",
        "conta_multiuser_convite",
        ["email_destino"],
        unique=False,
    )
    op.create_index(
        "uq_conta_convite_franquia_pendente",
        "conta_multiuser_convite",
        ["franquia_id"],
        unique=True,
        postgresql_where=sa.text("estado = 'pendente'"),
        sqlite_where=sa.text("estado = 'pendente'"),
    )
    op.create_index(
        "uq_conta_convite_conta_email_pendente",
        "conta_multiuser_convite",
        ["conta_id", "email_destino"],
        unique=True,
        postgresql_where=sa.text("estado = 'pendente'"),
        sqlite_where=sa.text("estado = 'pendente'"),
    )


def downgrade():
    op.drop_index(
        "uq_conta_convite_conta_email_pendente",
        table_name="conta_multiuser_convite",
    )
    op.drop_index(
        "uq_conta_convite_franquia_pendente",
        table_name="conta_multiuser_convite",
    )
    op.drop_index(
        "ix_conta_multiuser_convite_email_destino",
        table_name="conta_multiuser_convite",
    )
    op.drop_index(
        "ix_conta_multiuser_convite_estado",
        table_name="conta_multiuser_convite",
    )
    op.drop_index(
        "ix_conta_multiuser_convite_franquia_id",
        table_name="conta_multiuser_convite",
    )
    op.drop_index(
        "ix_conta_multiuser_convite_conta_id",
        table_name="conta_multiuser_convite",
    )
    op.drop_table("conta_multiuser_convite")
