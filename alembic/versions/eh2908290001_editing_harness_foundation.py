"""Editing harness foundation: revisioned drafts, harness runs and operations.

Revision ID: eh2908290001
Revises: ag2908290003
Create Date: 2026-08-29

Adds the substrate from docs/editing-harness-implementation-plan.md §12:

- `rough_cut_drafts` — one revisioned, checksummed draft row per project,
  replacing the untyped `ai_results` blob as the source of truth.
- `rough_cut_draft_revisions` — compressed full snapshots per revision.
- `editing_harness_runs` / `editing_harness_operations` — the transaction
  engine's run and operation records.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "eh2908290001"
down_revision = "ag2908290003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "rough_cut_drafts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "project_id",
            sa.Integer(),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "video_id",
            sa.Integer(),
            sa.ForeignKey("videos.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("checksum", sa.String(), nullable=True),
        sa.Column("payload", JSONB(), nullable=False, server_default="{}"),
        sa.Column("user_edited_at", sa.TIMESTAMP(), nullable=True),
        sa.Column("last_writer", sa.String(), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_rough_cut_drafts_id", "rough_cut_drafts", ["id"])
    op.create_index(
        "ix_rough_cut_drafts_project_id", "rough_cut_drafts", ["project_id"], unique=True
    )
    op.create_index("ix_rough_cut_drafts_video_id", "rough_cut_drafts", ["video_id"])

    op.create_table(
        "rough_cut_draft_revisions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "draft_id",
            sa.Integer(),
            sa.ForeignKey("rough_cut_drafts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("parent_revision", sa.Integer(), nullable=True),
        sa.Column("checksum", sa.String(), nullable=True),
        sa.Column("snapshot_zlib", sa.LargeBinary(), nullable=True),
        sa.Column("writer", sa.String(), nullable=True),
        sa.Column("source_id", sa.String(), nullable=True),
        sa.Column(
            "created_by",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("created_at", sa.TIMESTAMP(), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("draft_id", "revision", name="uq_rough_cut_draft_revision"),
    )
    op.create_index("ix_rough_cut_draft_revisions_id", "rough_cut_draft_revisions", ["id"])
    op.create_index(
        "ix_rough_cut_draft_revisions_draft_id", "rough_cut_draft_revisions", ["draft_id"]
    )

    op.create_table(
        "editing_harness_runs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "project_id",
            sa.Integer(),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "video_id",
            sa.Integer(),
            sa.ForeignKey("videos.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "workspace_id",
            sa.Integer(),
            sa.ForeignKey("workspaces.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "created_by",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("state", sa.String(), nullable=False, server_default="draft"),
        sa.Column("stage", sa.String(), nullable=True),
        sa.Column("intent", sa.Text(), nullable=True),
        sa.Column("recipe_id", sa.String(), nullable=True),
        sa.Column("recipe_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("params", JSONB(), nullable=True),
        sa.Column("base_draft_revision", sa.Integer(), nullable=True),
        sa.Column("applied_draft_revision", sa.Integer(), nullable=True),
        sa.Column("base_checksum", sa.String(), nullable=True),
        sa.Column("result_checksum", sa.String(), nullable=True),
        sa.Column("capability_snapshot", JSONB(), nullable=True),
        sa.Column("selection_snapshot", JSONB(), nullable=True),
        sa.Column("plan", JSONB(), nullable=True),
        sa.Column("plan_checksum", sa.String(), nullable=True),
        sa.Column("diff", JSONB(), nullable=True),
        sa.Column("estimates", JSONB(), nullable=True),
        sa.Column("applied_manifest", JSONB(), nullable=True),
        sa.Column("inverse_manifest", JSONB(), nullable=True),
        sa.Column("verification_report", JSONB(), nullable=True),
        sa.Column("warnings", JSONB(), nullable=True),
        sa.Column("model_provider", sa.String(), nullable=True),
        sa.Column("model_name", sa.String(), nullable=True),
        sa.Column("prompt_version", sa.String(), nullable=True),
        sa.Column("token_usage", JSONB(), nullable=True),
        sa.Column("cost_usd", sa.Float(), nullable=True),
        sa.Column("error_code", sa.String(), nullable=True),
        sa.Column("error_detail", sa.Text(), nullable=True),
        sa.Column("request_id", sa.String(), nullable=True),
        sa.Column("cancel_requested", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("planned_at", sa.TIMESTAMP(), nullable=True),
        sa.Column("approved_at", sa.TIMESTAMP(), nullable=True),
        sa.Column("applied_at", sa.TIMESTAMP(), nullable=True),
        sa.Column("verified_at", sa.TIMESTAMP(), nullable=True),
        sa.Column("reverted_at", sa.TIMESTAMP(), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_editing_harness_runs_id", "editing_harness_runs", ["id"])
    op.create_index("ix_editing_harness_runs_project_id", "editing_harness_runs", ["project_id"])
    op.create_index("ix_editing_harness_runs_video_id", "editing_harness_runs", ["video_id"])
    op.create_index(
        "ix_editing_harness_runs_workspace_id", "editing_harness_runs", ["workspace_id"]
    )
    op.create_index("ix_editing_harness_runs_created_by", "editing_harness_runs", ["created_by"])
    op.create_index("ix_editing_harness_runs_state", "editing_harness_runs", ["state"])
    op.create_index("ix_editing_harness_runs_recipe_id", "editing_harness_runs", ["recipe_id"])

    op.create_table(
        "editing_harness_operations",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "run_id",
            sa.Integer(),
            sa.ForeignKey("editing_harness_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("operation_key", sa.String(), nullable=False),
        sa.Column("type", sa.String(), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("sequence", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("depends_on", JSONB(), nullable=True),
        sa.Column("state", sa.String(), nullable=False, server_default="pending"),
        sa.Column("risk", sa.String(), nullable=False, server_default="reversible"),
        sa.Column("approval_group", sa.String(), nullable=True),
        sa.Column("target", JSONB(), nullable=True),
        sa.Column("preconditions", JSONB(), nullable=True),
        sa.Column("params", JSONB(), nullable=True),
        sa.Column("evidence", JSONB(), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("result", JSONB(), nullable=True),
        sa.Column("staged_asset", JSONB(), nullable=True),
        sa.Column("rollback", JSONB(), nullable=True),
        sa.Column("job_id", sa.String(), nullable=True),
        sa.Column("idempotency_key", sa.String(), nullable=True),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error_code", sa.String(), nullable=True),
        sa.Column("error_detail", sa.Text(), nullable=True),
        sa.Column("started_at", sa.TIMESTAMP(), nullable=True),
        sa.Column("completed_at", sa.TIMESTAMP(), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("run_id", "operation_key", name="uq_harness_operation_key"),
        sa.UniqueConstraint("idempotency_key", name="uq_harness_operation_idem"),
    )
    op.create_index("ix_editing_harness_operations_id", "editing_harness_operations", ["id"])
    op.create_index(
        "ix_editing_harness_operations_run_id", "editing_harness_operations", ["run_id"]
    )
    op.create_index(
        "ix_editing_harness_operations_type", "editing_harness_operations", ["type"]
    )
    op.create_index(
        "ix_editing_harness_operations_state", "editing_harness_operations", ["state"]
    )


def downgrade() -> None:
    op.drop_table("editing_harness_operations")
    op.drop_table("editing_harness_runs")
    op.drop_table("rough_cut_draft_revisions")
    op.drop_table("rough_cut_drafts")
