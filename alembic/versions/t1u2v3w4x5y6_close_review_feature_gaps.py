"""close review feature gaps

Revision ID: t1u2v3w4x5y6
Revises: s9t0u1v2w3x4
Create Date: 2026-04-14
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "t1u2v3w4x5y6"
down_revision = "s9t0u1v2w3x4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "review_links",
        sa.Column(
            "approval_required_for_download",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
    )
    op.add_column("review_links", sa.Column("version_group_id", sa.String(), nullable=True))
    op.add_column("review_links", sa.Column("version_label", sa.String(), nullable=True))
    op.create_index(
        "ix_review_links_version_group_id",
        "review_links",
        ["version_group_id"],
        unique=False,
    )

    op.add_column("review_sessions", sa.Column("guest_avatar_url", sa.String(), nullable=True))

    op.create_table(
        "review_magic_tokens",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("review_link_id", sa.Integer(), nullable=False),
        sa.Column("email", sa.String(), nullable=False),
        sa.Column("token_hash", sa.String(), nullable=False),
        sa.Column("fingerprint", sa.String(), nullable=True),
        sa.Column("ip_address", sa.String(), nullable=True),
        sa.Column("expires_at", sa.TIMESTAMP(), nullable=False),
        sa.Column("used_at", sa.TIMESTAMP(), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["review_link_id"], ["review_links.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_review_magic_tokens_email", "review_magic_tokens", ["email"], unique=False)
    op.create_index("ix_review_magic_tokens_token_hash", "review_magic_tokens", ["token_hash"], unique=True)
    op.create_index("ix_review_magic_tokens_fingerprint", "review_magic_tokens", ["fingerprint"], unique=False)
    op.create_index("ix_review_magic_tokens_review_link_id", "review_magic_tokens", ["review_link_id"], unique=False)

    op.create_table(
        "review_signoffs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("review_link_id", sa.Integer(), nullable=False),
        sa.Column("session_id", sa.Integer(), nullable=False),
        sa.Column("signer_name", sa.String(), nullable=True),
        sa.Column("signer_email", sa.String(), nullable=True),
        sa.Column("declaration_text", sa.Text(), nullable=False),
        sa.Column("legal_snapshot_json", JSONB(), nullable=True),
        sa.Column("signed_at", sa.TIMESTAMP(), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["review_link_id"], ["review_links.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["session_id"], ["review_sessions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_review_signoffs_review_link_id", "review_signoffs", ["review_link_id"], unique=False)
    op.create_index("ix_review_signoffs_session_id", "review_signoffs", ["session_id"], unique=False)

    op.create_table(
        "review_comment_drafts",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("review_link_id", sa.Integer(), nullable=False),
        sa.Column("session_id", sa.Integer(), nullable=False),
        sa.Column("video_id", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("timecode", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["review_link_id"], ["review_links.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["session_id"], ["review_sessions.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["video_id"], ["videos.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_review_comment_drafts_review_link_id", "review_comment_drafts", ["review_link_id"], unique=False)
    op.create_index("ix_review_comment_drafts_session_id", "review_comment_drafts", ["session_id"], unique=False)
    op.create_index("ix_review_comment_drafts_video_id", "review_comment_drafts", ["video_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_review_comment_drafts_video_id", table_name="review_comment_drafts")
    op.drop_index("ix_review_comment_drafts_session_id", table_name="review_comment_drafts")
    op.drop_index("ix_review_comment_drafts_review_link_id", table_name="review_comment_drafts")
    op.drop_table("review_comment_drafts")

    op.drop_index("ix_review_signoffs_session_id", table_name="review_signoffs")
    op.drop_index("ix_review_signoffs_review_link_id", table_name="review_signoffs")
    op.drop_table("review_signoffs")

    op.drop_index("ix_review_magic_tokens_review_link_id", table_name="review_magic_tokens")
    op.drop_index("ix_review_magic_tokens_fingerprint", table_name="review_magic_tokens")
    op.drop_index("ix_review_magic_tokens_token_hash", table_name="review_magic_tokens")
    op.drop_index("ix_review_magic_tokens_email", table_name="review_magic_tokens")
    op.drop_table("review_magic_tokens")

    op.drop_column("review_sessions", "guest_avatar_url")

    op.drop_index("ix_review_links_version_group_id", table_name="review_links")
    op.drop_column("review_links", "version_label")
    op.drop_column("review_links", "version_group_id")
    op.drop_column("review_links", "approval_required_for_download")
