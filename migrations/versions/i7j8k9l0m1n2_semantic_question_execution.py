"""execucao semantica duravel sem provider real

Revision ID: i7j8k9l0m1n2
Revises: h6i7j8k9l0m1
Create Date: 2026-10-09

Persistencia compartilhada da pergunta semantica: execucao, journal,
revisao humana e teto por job. Nao cria tentativa financeira.
"""
from alembic import op
import sqlalchemy as sa


revision = "i7j8k9l0m1n2"
down_revision = "h6i7j8k9l0m1"
branch_labels = None
depends_on = None

_STATUSES = (
    "prepared",
    "claimed",
    "dispatch_ready",
    "execution_started",
    "response_observed",
    "validated",
    "proposal_recorded",
    "requires_review",
    "accepted",
    "rejected",
    "blocked",
    "failed",
    "uncertain",
    "stale",
)


def upgrade():
    op.create_table(
        "semantic_question_execution",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("tenant_scope", sa.String(length=160), nullable=False),
        sa.Column("semantic_question_key", sa.String(length=160), nullable=False),
        sa.Column("decision_dependency_fingerprint", sa.String(length=80), nullable=False),
        sa.Column("ai_execution_fingerprint", sa.String(length=80), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("question_attempt_key", sa.String(length=160), nullable=False),
        sa.Column("status", sa.String(length=40), nullable=False),
        sa.Column("claim_token", sa.String(length=64), nullable=True),
        sa.Column("fencing_version", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("claimed_at", sa.DateTime(), nullable=True),
        sa.Column("claim_expires_at", sa.DateTime(), nullable=True),
        sa.Column("job_ref", sa.String(length=160), nullable=False),
        sa.Column("question_type", sa.String(length=80), nullable=False),
        sa.Column("question_id", sa.String(length=80), nullable=False),
        sa.Column("manifest_digest", sa.String(length=80), nullable=True),
        sa.Column("manifest_payload", sa.Text(), nullable=True),
        sa.Column("evidence_manifest", sa.Text(), nullable=True),
        sa.Column("snapshot_payload", sa.Text(), nullable=True),
        sa.Column("batch_request_key", sa.String(length=160), nullable=True),
        sa.Column("financial_attempt_key", sa.String(length=160), nullable=True),
        sa.Column("execution_started_at", sa.DateTime(), nullable=True),
        sa.Column("response_observed_at", sa.DateTime(), nullable=True),
        sa.Column("response_payload", sa.Text(), nullable=True),
        sa.Column("validation_status", sa.String(length=40), nullable=True),
        sa.Column("validation_findings", sa.Text(), nullable=True),
        sa.Column("proposal_ref", sa.String(length=80), nullable=True),
        sa.Column("proposal_payload", sa.Text(), nullable=True),
        sa.Column("proposal_applicable", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("stale_reason", sa.String(length=120), nullable=True),
        sa.Column("budget_state", sa.String(length=20), nullable=False, server_default="none"),
        sa.Column("reserved_credit_amount", sa.Numeric(18, 6), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("generation >= 1", name="ck_sqe_generation"),
        sa.CheckConstraint("fencing_version >= 0", name="ck_sqe_fencing"),
        sa.CheckConstraint(
            "status IN (" + ", ".join(repr(item) for item in _STATUSES) + ")",
            name="ck_sqe_status",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_scope",
            "semantic_question_key",
            "decision_dependency_fingerprint",
            "ai_execution_fingerprint",
            "generation",
            name="uq_sqe_identity",
        ),
        sa.UniqueConstraint("question_attempt_key", name="uq_sqe_attempt_key"),
    )
    op.create_index("ix_sqe_tenant_scope", "semantic_question_execution", ["tenant_scope"])
    op.create_index("ix_sqe_status", "semantic_question_execution", ["status"])
    op.create_index("ix_sqe_job_ref", "semantic_question_execution", ["job_ref"])
    op.create_table(
        "semantic_question_journal",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("execution_id", sa.Integer(), nullable=False),
        sa.Column("checkpoint", sa.String(length=40), nullable=False),
        sa.Column("manifest_digest", sa.String(length=80), nullable=True),
        sa.Column("response_digest", sa.String(length=80), nullable=True),
        sa.Column("payload", sa.Text(), nullable=True),
        sa.Column("claim_token", sa.String(length=64), nullable=True),
        sa.Column("fencing_version", sa.Integer(), nullable=False),
        sa.Column("applicable", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("stale_reason", sa.String(length=120), nullable=True),
        sa.Column("dependency_fingerprint", sa.String(length=80), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["execution_id"],
            ["semantic_question_execution.id"],
            name="fk_sqj_execution",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_sqj_execution_id", "semantic_question_journal", ["execution_id"])
    op.create_index("ix_sqj_checkpoint", "semantic_question_journal", ["checkpoint"])
    op.create_table(
        "semantic_human_review",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("tenant_scope", sa.String(length=160), nullable=False),
        sa.Column("semantic_question_key", sa.String(length=160), nullable=False),
        sa.Column("decision_dependency_fingerprint", sa.String(length=80), nullable=False),
        sa.Column("material_fingerprint", sa.String(length=80), nullable=False),
        sa.Column("selected_candidate_ref", sa.String(length=160), nullable=True),
        sa.Column("review_state", sa.String(length=20), nullable=False),
        sa.Column("reviewer_ref", sa.String(length=160), nullable=False),
        sa.Column("review_revision", sa.Integer(), nullable=False),
        sa.Column("evidence_refs", sa.Text(), nullable=True),
        sa.Column("proposal_ref", sa.String(length=80), nullable=True),
        sa.Column("provenance", sa.Text(), nullable=True),
        sa.Column("execution_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "review_state IN ('accepted', 'rejected', 'reopened')",
            name="ck_shr_review_state",
        ),
        sa.CheckConstraint("review_revision >= 1", name="ck_shr_revision"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_scope",
            "semantic_question_key",
            "decision_dependency_fingerprint",
            "material_fingerprint",
            "review_revision",
            name="uq_shr_revision",
        ),
    )
    op.create_index("ix_shr_tenant_scope", "semantic_human_review", ["tenant_scope"])
    op.create_index("ix_shr_review_state", "semantic_human_review", ["review_state"])
    op.create_index("ix_shr_execution_id", "semantic_human_review", ["execution_id"])
    op.create_table(
        "semantic_ai_job_budget",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("tenant_scope", sa.String(length=160), nullable=False),
        sa.Column("job_ref", sa.String(length=160), nullable=False),
        sa.Column("max_requests", sa.Integer(), nullable=True),
        sa.Column("max_billable_credits", sa.Numeric(18, 6), nullable=True),
        sa.Column("reserved_requests", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("completed_requests", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("reserved_credits", sa.Numeric(18, 6), nullable=False, server_default="0"),
        sa.Column("settled_credits", sa.Numeric(18, 6), nullable=False, server_default="0"),
        sa.Column("uncertain_credits", sa.Numeric(18, 6), nullable=False, server_default="0"),
        sa.Column("version", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("reserved_requests >= 0", name="ck_sajb_reserved_requests"),
        sa.CheckConstraint("completed_requests >= 0", name="ck_sajb_completed_requests"),
        sa.CheckConstraint("reserved_credits >= 0", name="ck_sajb_reserved_credits"),
        sa.CheckConstraint("settled_credits >= 0", name="ck_sajb_settled_credits"),
        sa.CheckConstraint("uncertain_credits >= 0", name="ck_sajb_uncertain_credits"),
        sa.CheckConstraint("version >= 0", name="ck_sajb_version"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_scope", "job_ref", name="uq_sajb_tenant_job"),
    )


def downgrade():
    op.drop_table("semantic_ai_job_budget")
    op.drop_index("ix_shr_execution_id", table_name="semantic_human_review")
    op.drop_index("ix_shr_review_state", table_name="semantic_human_review")
    op.drop_index("ix_shr_tenant_scope", table_name="semantic_human_review")
    op.drop_table("semantic_human_review")
    op.drop_index("ix_sqj_checkpoint", table_name="semantic_question_journal")
    op.drop_index("ix_sqj_execution_id", table_name="semantic_question_journal")
    op.drop_table("semantic_question_journal")
    op.drop_index("ix_sqe_job_ref", table_name="semantic_question_execution")
    op.drop_index("ix_sqe_status", table_name="semantic_question_execution")
    op.drop_index("ix_sqe_tenant_scope", table_name="semantic_question_execution")
    op.drop_table("semantic_question_execution")
