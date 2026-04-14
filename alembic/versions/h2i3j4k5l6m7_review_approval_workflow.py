"""review approval workflow: comment kind/status, signoff pdf fields, workflows, export flag

Revision ID: h2i3j4k5l6m7
Revises: b1c2d3e4f5g6
Create Date: 2026-04-15
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "h2i3j4k5l6m7"
down_revision = "b1c2d3e4f5g6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "comments",
        sa.Column("kind", sa.String(), server_default="comment", nullable=False),
    )
    op.add_column(
        "comments",
        sa.Column("status", sa.String(), server_default="open", nullable=False),
    )
    op.add_column(
        "comments",
        sa.Column("assignee_user_id", sa.Integer(), nullable=True),
    )
    op.add_column(
        "comments",
        sa.Column("status_changed_at", sa.TIMESTAMP(), nullable=True),
    )
    op.create_foreign_key(
        "fk_comments_assignee_user_id",
        "comments",
        "users",
        ["assignee_user_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index("ix_comments_assignee_user_id", "comments", ["assignee_user_id"], unique=False)
    op.create_index("ix_comments_kind", "comments", ["kind"], unique=False)
    op.create_index("ix_comments_status", "comments", ["status"], unique=False)

    op.execute(
        """
        UPDATE comments SET status = 'resolved'
        WHERE is_resolved = true AND (status IS NULL OR status = 'open')
        """
    )

    op.add_column(
        "review_signoffs",
        sa.Column("signature_type", sa.String(), server_default="none", nullable=False),
    )
    op.add_column("review_signoffs", sa.Column("typed_signature", sa.Text(), nullable=True))
    op.add_column("review_signoffs", sa.Column("signature_image_data", sa.Text(), nullable=True))
    op.add_column("review_signoffs", sa.Column("pdf_url", sa.String(), nullable=True))

    op.add_column(
        "review_links",
        sa.Column("allow_export", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )

    op.create_table(
        "review_workflow_templates",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("project_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_review_workflow_templates_project_id",
        "review_workflow_templates",
        ["project_id"],
        unique=False,
    )

    op.create_table(
        "review_workflow_stages",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("template_id", sa.Integer(), nullable=False),
        sa.Column("stage_index", sa.Integer(), nullable=False),
        sa.Column("stage_key", sa.String(), nullable=False),
        sa.Column("label", sa.String(), nullable=False),
        sa.Column("notify_user_ids", JSONB(), server_default="[]", nullable=False),
        sa.ForeignKeyConstraint(["template_id"], ["review_workflow_templates.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_review_workflow_stages_template_id",
        "review_workflow_stages",
        ["template_id"],
        unique=False,
    )

    op.create_table(
        "review_workflow_runs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("review_link_id", sa.Integer(), nullable=False),
        sa.Column("template_id", sa.Integer(), nullable=False),
        sa.Column("current_stage_index", sa.Integer(), server_default="0", nullable=False),
        sa.Column("completed_at", sa.TIMESTAMP(), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["review_link_id"], ["review_links.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["template_id"], ["review_workflow_templates.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_review_workflow_runs_review_link_id",
        "review_workflow_runs",
        ["review_link_id"],
        unique=True,
    )
    op.create_index(
        "ix_review_workflow_runs_template_id",
        "review_workflow_runs",
        ["template_id"],
        unique=False,
    )

    op.add_column(
        "user_settings",
        sa.Column("email_mention_digest", sa.String(), server_default="off", nullable=False),
    )


def downgrade() -> None:
    op.drop_column("user_settings", "email_mention_digest")

    op.drop_index("ix_review_workflow_runs_template_id", table_name="review_workflow_runs")
    op.drop_index("ix_review_workflow_runs_review_link_id", table_name="review_workflow_runs")
    op.drop_table("review_workflow_runs")

    op.drop_index("ix_review_workflow_stages_template_id", table_name="review_workflow_stages")
    op.drop_table("review_workflow_stages")

    op.drop_index("ix_review_workflow_templates_project_id", table_name="review_workflow_templates")
    op.drop_table("review_workflow_templates")

    op.drop_column("review_links", "allow_export")

    op.drop_column("review_signoffs", "pdf_url")
    op.drop_column("review_signoffs", "signature_image_data")
    op.drop_column("review_signoffs", "typed_signature")
    op.drop_column("review_signoffs", "signature_type")

    op.drop_index("ix_comments_status", table_name="comments")
    op.drop_index("ix_comments_kind", table_name="comments")
    op.drop_index("ix_comments_assignee_user_id", table_name="comments")
    op.drop_constraint("fk_comments_assignee_user_id", "comments", type_="foreignkey")
    op.drop_column("comments", "status_changed_at")
    op.drop_column("comments", "assignee_user_id")
    op.drop_column("comments", "status")
    op.drop_column("comments", "kind")
