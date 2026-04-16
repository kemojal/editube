"""add comment attachments table

Revision ID: s1t2u3v4w5x6
Revises: r1s2t3u4v5w6
Create Date: 2026-04-15 19:15:00.000000
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


# revision identifiers, used by Alembic.
revision = "s1t2u3v4w5x6"
down_revision = "r1s2t3u4v5w6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    table_names = set(inspector.get_table_names())

    if "comment_attachments" not in table_names:
        op.create_table(
            "comment_attachments",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("comment_id", sa.Integer(), nullable=False),
            sa.Column("attachment_type", sa.String(), nullable=False),
            sa.Column("file_url", sa.Text(), nullable=False),
            sa.Column("mime_type", sa.String(), nullable=True),
            sa.Column("duration_ms", sa.Integer(), nullable=True),
            sa.Column("bytes_size", sa.Integer(), nullable=True),
            sa.Column("waveform", JSONB(), nullable=True),
            sa.Column("transcript", sa.Text(), nullable=True),
            sa.Column("created_at", sa.TIMESTAMP(), server_default=sa.text("now()"), nullable=False),
            sa.ForeignKeyConstraint(["comment_id"], ["comments.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index(op.f("ix_comment_attachments_id"), "comment_attachments", ["id"], unique=False)
        op.create_index(
            op.f("ix_comment_attachments_comment_id"),
            "comment_attachments",
            ["comment_id"],
            unique=False,
        )
        op.create_index(
            op.f("ix_comment_attachments_attachment_type"),
            "comment_attachments",
            ["attachment_type"],
            unique=False,
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    table_names = set(inspector.get_table_names())

    if "comment_attachments" in table_names:
        op.drop_index(op.f("ix_comment_attachments_attachment_type"), table_name="comment_attachments")
        op.drop_index(op.f("ix_comment_attachments_comment_id"), table_name="comment_attachments")
        op.drop_index(op.f("ix_comment_attachments_id"), table_name="comment_attachments")
        op.drop_table("comment_attachments")
