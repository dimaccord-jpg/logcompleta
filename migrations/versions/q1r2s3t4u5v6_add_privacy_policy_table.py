"""add privacy_policy table

Revision ID: q1r2s3t4u5v6
Revises: p9q0r1s2t3u4
Create Date: 2026-05-07 16:40:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "q1r2s3t4u5v6"
down_revision = "p9q0r1s2t3u4"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "privacy_policy",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("filename", sa.String(length=255), nullable=False),
        sa.Column("original_filename", sa.String(length=255), nullable=True),
        sa.Column("upload_date", sa.DateTime(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("uploaded_by_user_id", sa.Integer(), nullable=True),
        sa.Column("file_size_bytes", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("mime_type", sa.String(length=120), nullable=True),
        sa.ForeignKeyConstraint(["uploaded_by_user_id"], ["user.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("privacy_policy", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_privacy_policy_is_active"), ["is_active"], unique=False)
        batch_op.create_index(
            batch_op.f("ix_privacy_policy_uploaded_by_user_id"),
            ["uploaded_by_user_id"],
            unique=False,
        )


def downgrade():
    with op.batch_alter_table("privacy_policy", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_privacy_policy_uploaded_by_user_id"))
        batch_op.drop_index(batch_op.f("ix_privacy_policy_is_active"))

    op.drop_table("privacy_policy")
