"""add google drive connections + drive imports

Revision ID: d5e6f7a8b9c0
Revises: c4d5e6f7a8b9
Create Date: 2026-07-27

Backs the "import from Google Drive" source in the create-project wizard.
See docs/google-drive-import-plan.md.
"""

from alembic import op
import sqlalchemy as sa

revision = "d5e6f7a8b9c0"
down_revision = "c4d5e6f7a8b9"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "user_google_drive_connections",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("google_sub", sa.String(), nullable=False),
        sa.Column("email", sa.String(), nullable=True),
        sa.Column("picture_url", sa.String(), nullable=True),
        sa.Column("refresh_token_encrypted", sa.Text(), nullable=False),
        sa.Column("access_token", sa.Text(), nullable=True),
        sa.Column("access_expires_at", sa.TIMESTAMP(), nullable=True),
        sa.Column("scopes", sa.Text(), nullable=True),
        sa.Column("status", sa.String(), server_default="active", nullable=False),
        sa.Column("is_default", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "google_sub", name="uq_google_drive_conn_user_sub"),
    )
    op.create_index(
        op.f("ix_user_google_drive_connections_id"), "user_google_drive_connections", ["id"], unique=False
    )
    op.create_index(
        op.f("ix_user_google_drive_connections_user_id"),
        "user_google_drive_connections",
        ["user_id"],
        unique=False,
    )

    op.create_table(
        "drive_imports",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("connection_id", sa.Integer(), nullable=False),
        sa.Column("drive_file_id", sa.String(), nullable=False),
        sa.Column("file_name", sa.String(), nullable=True),
        sa.Column("mime_type", sa.String(), nullable=True),
        sa.Column("total_bytes", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("bytes_transferred", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("progress_percent", sa.Integer(), server_default="0", nullable=False),
        sa.Column("duration_seconds", sa.Integer(), nullable=True),
        sa.Column("thumbnail_url", sa.String(), nullable=True),
        sa.Column("status", sa.String(), server_default="queued", nullable=False),
        sa.Column("file_path", sa.Text(), nullable=True),
        sa.Column("error_code", sa.String(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["connection_id"], ["user_google_drive_connections.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_drive_imports_id"), "drive_imports", ["id"], unique=False)
    op.create_index(op.f("ix_drive_imports_user_id"), "drive_imports", ["user_id"], unique=False)
    op.create_index(
        op.f("ix_drive_imports_connection_id"), "drive_imports", ["connection_id"], unique=False
    )
    op.create_index(
        op.f("ix_drive_imports_drive_file_id"), "drive_imports", ["drive_file_id"], unique=False
    )
    op.create_index(op.f("ix_drive_imports_status"), "drive_imports", ["status"], unique=False)


def downgrade():
    op.drop_index(op.f("ix_drive_imports_status"), table_name="drive_imports")
    op.drop_index(op.f("ix_drive_imports_drive_file_id"), table_name="drive_imports")
    op.drop_index(op.f("ix_drive_imports_connection_id"), table_name="drive_imports")
    op.drop_index(op.f("ix_drive_imports_user_id"), table_name="drive_imports")
    op.drop_index(op.f("ix_drive_imports_id"), table_name="drive_imports")
    op.drop_table("drive_imports")

    op.drop_index(
        op.f("ix_user_google_drive_connections_user_id"), table_name="user_google_drive_connections"
    )
    op.drop_index(
        op.f("ix_user_google_drive_connections_id"), table_name="user_google_drive_connections"
    )
    op.drop_table("user_google_drive_connections")
