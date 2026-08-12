"""lead acquisition campaign fields

Revision ID: s3t4u5v6w7x8
Revises: 462e6ffdc929
Create Date: 2026-08-10 15:20:00.000000

Campos aditivos em leads para jornada de aquisição (Ads → Lead → CTA → Cadastro).
Não altera email/data_inscricao; não faz backfill de campanha.
"""
from alembic import op
import sqlalchemy as sa


revision = "s3t4u5v6w7x8"
down_revision = "462e6ffdc929"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("leads", schema=None) as batch_op:
        batch_op.add_column(sa.Column("acquisition_campaign", sa.String(length=80), nullable=True))
        batch_op.add_column(sa.Column("acquisition_source", sa.String(length=50), nullable=True))
        batch_op.add_column(sa.Column("campaign_captured_at", sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column("cta_email_sent_at", sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column("cta_clicked_at", sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column("converted_user_id", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("converted_at", sa.DateTime(), nullable=True))
        batch_op.add_column(
            sa.Column(
                "followup_count",
                sa.Integer(),
                nullable=False,
                server_default=sa.text("0"),
            )
        )
        batch_op.add_column(sa.Column("last_followup_sent_at", sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column("opt_out_at", sa.DateTime(), nullable=True))
        batch_op.create_index(
            "ix_leads_acquisition_campaign_captured_at",
            ["acquisition_campaign", "campaign_captured_at"],
            unique=False,
        )


def downgrade():
    with op.batch_alter_table("leads", schema=None) as batch_op:
        batch_op.drop_index("ix_leads_acquisition_campaign_captured_at")
        batch_op.drop_column("opt_out_at")
        batch_op.drop_column("last_followup_sent_at")
        batch_op.drop_column("followup_count")
        batch_op.drop_column("converted_at")
        batch_op.drop_column("converted_user_id")
        batch_op.drop_column("cta_clicked_at")
        batch_op.drop_column("cta_email_sent_at")
        batch_op.drop_column("campaign_captured_at")
        batch_op.drop_column("acquisition_source")
        batch_op.drop_column("acquisition_campaign")
