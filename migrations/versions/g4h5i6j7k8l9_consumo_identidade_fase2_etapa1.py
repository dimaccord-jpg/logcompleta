"""fase 2 etapa 1: colunas identidade de negocio em ia_consumo_evento e processing_events

Revision ID: g4h5i6j7k8l9
Revises: f3a4b5c6d7e8
Create Date: 2026-04-03

Decisao: colunas todas NULLable e sem backfill — eventos antigos permanecem sem identidade
preenchida; apenas consumo novo passa a gravar conta/franquia/usuario/tipo_origem/origem_sistema.
Indices leves em campos de filtro (usuario_id, tipo_origem) para analises futuras.
"""
from alembic import op
import sqlalchemy as sa


revision = "g4h5i6j7k8l9"
down_revision = "f3a4b5c6d7e8"
branch_labels = None
depends_on = None


def upgrade():
    # PostgreSQL aceita BOOLEAN nullable; SQLite tambem via SQLAlchemy.
    for table in ("ia_consumo_evento", "processing_events"):
        with op.batch_alter_table(table, schema=None) as batch_op:
            batch_op.add_column(sa.Column("conta_id", sa.Integer(), nullable=True))
            batch_op.add_column(sa.Column("franquia_id", sa.Integer(), nullable=True))
            batch_op.add_column(sa.Column("usuario_id", sa.Integer(), nullable=True))
            batch_op.add_column(sa.Column("tipo_origem", sa.String(length=80), nullable=True))
            batch_op.add_column(sa.Column("origem_sistema", sa.Boolean(), nullable=True))
        op.create_index(f"ix_{table}_conta_id", table, ["conta_id"], unique=False)
        op.create_index(f"ix_{table}_franquia_id", table, ["franquia_id"], unique=False)
        op.create_index(f"ix_{table}_usuario_id", table, ["usuario_id"], unique=False)
        op.create_index(f"ix_{table}_tipo_origem", table, ["tipo_origem"], unique=False)
        op.create_index(f"ix_{table}_origem_sistema", table, ["origem_sistema"], unique=False)


def downgrade():
    for table in ("processing_events", "ia_consumo_evento"):
        for ix in (
            f"ix_{table}_origem_sistema",
            f"ix_{table}_tipo_origem",
            f"ix_{table}_usuario_id",
            f"ix_{table}_franquia_id",
            f"ix_{table}_conta_id",
        ):
            op.drop_index(ix, table_name=table)
        with op.batch_alter_table(table, schema=None) as batch_op:
            batch_op.drop_column("origem_sistema")
            batch_op.drop_column("tipo_origem")
            batch_op.drop_column("usuario_id")
            batch_op.drop_column("franquia_id")
            batch_op.drop_column("conta_id")
