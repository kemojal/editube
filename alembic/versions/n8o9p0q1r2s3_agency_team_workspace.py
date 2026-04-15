"""Agency & team: workspaces, members, branding, templates, assets, project workspace FK, comment visibility.

Revision ID: n8o9p0q1r2s3
Revises: h2i3j4k5l6m7
Create Date: 2026-04-15
"""

from __future__ import annotations

import json
import secrets

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy import text

revision = "n8o9p0q1r2s3"
down_revision = "h2i3j4k5l6m7"
branch_labels = None
depends_on = None


def _slug_for_uid(uid: int) -> str:
    return f"ws-{uid}-{secrets.token_hex(4)}"


SYSTEM_TEMPLATES = [
    (
        "wedding",
        "Wedding",
        {
            "folders": [
                {"name": "Ceremony", "children": []},
                {"name": "Reception", "children": []},
                {"name": "Speeches", "children": []},
                {"name": "Deliverables", "children": [{"name": "Teasers"}]},
            ],
            "workflow_template": {
                "name": "Wedding delivery",
                "stages": [
                    {"stage_key": "editor_cut", "label": "Editor rough cut", "notify_user_ids": []},
                    {"stage_key": "producer_qc", "label": "Producer QC", "notify_user_ids": []},
                    {"stage_key": "client_review", "label": "Client review", "notify_user_ids": []},
                ],
            },
        },
    ),
    (
        "podcast",
        "Podcast",
        {
            "folders": [
                {"name": "Raw audio", "children": []},
                {"name": "Music & SFX", "children": []},
                {"name": "Video cut", "children": []},
                {"name": "Exports", "children": []},
            ],
            "workflow_template": {
                "name": "Podcast review",
                "stages": [
                    {"stage_key": "host_preview", "label": "Host preview", "notify_user_ids": []},
                    {"stage_key": "client_review", "label": "Sponsor / client review", "notify_user_ids": []},
                ],
            },
        },
    ),
    (
        "youtube_long",
        "YouTube long-form",
        {
            "folders": [
                {"name": "A-roll", "children": []},
                {"name": "B-roll", "children": []},
                {"name": "Graphics", "children": []},
                {"name": "Exports", "children": [{"name": "Thumbnail refs"}]},
            ],
            "workflow_template": {
                "name": "YouTube review",
                "stages": [
                    {"stage_key": "internal", "label": "Internal cut", "notify_user_ids": []},
                    {"stage_key": "creator_review", "label": "Creator review", "notify_user_ids": []},
                ],
            },
        },
    ),
    (
        "ad_spot",
        "Ad spot",
        {
            "folders": [
                {"name": "Brand", "children": [{"name": "Logos"}]},
                {"name": "Rough", "children": []},
                {"name": "Finishing", "children": []},
                {"name": "Deliverables", "children": []},
            ],
            "workflow_template": {
                "name": "Ad approval",
                "stages": [
                    {"stage_key": "agency_internal", "label": "Agency internal", "notify_user_ids": []},
                    {"stage_key": "client_legal", "label": "Client / legal", "notify_user_ids": []},
                ],
            },
        },
    ),
]


def upgrade() -> None:
    op.create_table(
        "workspaces",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("slug", sa.String(), nullable=False),
        sa.Column("owner_user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("settings", JSONB, nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_workspaces_slug", "workspaces", ["slug"], unique=True)
    op.create_index("ix_workspaces_owner_user_id", "workspaces", ["owner_user_id"])

    op.create_table(
        "workspace_members",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("workspace_id", sa.Integer(), sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("role", sa.String(), server_default="editor", nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("workspace_id", "user_id", name="uq_workspace_members_ws_user"),
    )
    op.create_index("ix_workspace_members_workspace_id", "workspace_members", ["workspace_id"])
    op.create_index("ix_workspace_members_user_id", "workspace_members", ["user_id"])

    op.create_table(
        "workspace_brandings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("workspace_id", sa.Integer(), sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False),
        sa.Column("logo_url", sa.String(), nullable=True),
        sa.Column("primary_color", sa.String(), nullable=True),
        sa.Column("accent_color", sa.String(), nullable=True),
        sa.Column("client_footer_text", sa.Text(), nullable=True),
        sa.Column("custom_domain", sa.String(), nullable=True),
        sa.Column("domain_verification_token", sa.String(), nullable=True),
        sa.Column("domain_verified_at", sa.TIMESTAMP(), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_workspace_brandings_workspace_id", "workspace_brandings", ["workspace_id"], unique=True)
    op.create_index("ix_workspace_brandings_custom_domain", "workspace_brandings", ["custom_domain"], unique=True)

    op.create_table(
        "workspace_invites",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("workspace_id", sa.Integer(), sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False),
        sa.Column("email", sa.String(), nullable=False),
        sa.Column("role", sa.String(), server_default="editor", nullable=False),
        sa.Column("token", sa.String(), nullable=False),
        sa.Column("invited_by_user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("expires_at", sa.TIMESTAMP(), nullable=False),
        sa.Column("accepted_at", sa.TIMESTAMP(), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_workspace_invites_workspace_id", "workspace_invites", ["workspace_id"])
    op.create_index("ix_workspace_invites_email", "workspace_invites", ["email"])
    op.create_index("ix_workspace_invites_token", "workspace_invites", ["token"], unique=True)

    op.create_table(
        "project_templates",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("workspace_id", sa.Integer(), sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=True),
        sa.Column("template_key", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("definition", JSONB, nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_project_templates_workspace_id", "project_templates", ["workspace_id"])
    op.create_index("ix_project_templates_template_key", "project_templates", ["template_key"])
    op.create_index(
        "uq_project_templates_system_key",
        "project_templates",
        ["template_key"],
        unique=True,
        postgresql_where=sa.text("workspace_id IS NULL"),
    )

    op.create_table(
        "workspace_assets",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("workspace_id", sa.Integer(), sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False),
        sa.Column("category", sa.String(), nullable=False),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("file_url", sa.String(), nullable=False),
        sa.Column("extra", JSONB, nullable=True),
        sa.Column("created_by_user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_workspace_assets_workspace_id", "workspace_assets", ["workspace_id"])
    op.create_index("ix_workspace_assets_category", "workspace_assets", ["category"])

    op.add_column(
        "projects",
        sa.Column("workspace_id", sa.Integer(), sa.ForeignKey("workspaces.id", ondelete="RESTRICT"), nullable=True),
    )
    op.add_column(
        "projects",
        sa.Column(
            "created_from_template_id",
            sa.Integer(),
            sa.ForeignKey("project_templates.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.create_index("ix_projects_workspace_id", "projects", ["workspace_id"])

    op.add_column(
        "comments",
        sa.Column("visibility", sa.String(), server_default="public", nullable=False),
    )
    op.add_column("comments", sa.Column("due_at", sa.TIMESTAMP(), nullable=True))
    op.create_index("ix_comments_visibility", "comments", ["visibility"])

    conn = op.get_bind()

    users = conn.execute(text("SELECT id FROM users ORDER BY id")).fetchall()
    user_workspace: dict[int, int] = {}

    for (uid,) in users:
        row = conn.execute(
            text("SELECT workspace_name FROM user_settings WHERE user_id = :uid LIMIT 1"),
            {"uid": uid},
        ).fetchone()
        wname = (row[0] if row else None) or "My Workspace"
        slug = _slug_for_uid(int(uid))
        row = conn.execute(
            text(
                """
                INSERT INTO workspaces (name, slug, owner_user_id, created_at, updated_at)
                VALUES (:name, :slug, :uid, NOW(), NOW())
                RETURNING id
                """
            ),
            {"name": wname, "slug": slug, "uid": uid},
        ).fetchone()
        wid = int(row[0])
        user_workspace[int(uid)] = wid
        conn.execute(
            text(
                """
                INSERT INTO workspace_members (workspace_id, user_id, role, created_at)
                VALUES (:wid, :uid, 'owner', NOW())
                """
            ),
            {"wid": wid, "uid": uid},
        )
        conn.execute(
            text(
                """
                INSERT INTO workspace_brandings (workspace_id, created_at, updated_at)
                VALUES (:wid, NOW(), NOW())
                """
            ),
            {"wid": wid},
        )

    for uid, wid in user_workspace.items():
        conn.execute(
            text("UPDATE projects SET workspace_id = :wid WHERE creator_id = :uid"),
            {"wid": wid, "uid": uid},
        )

    conn.execute(
        text(
            """
            UPDATE projects SET workspace_id = (
                SELECT w.id FROM workspaces w
                JOIN users u ON u.id = projects.creator_id
                JOIN workspace_members wm ON wm.workspace_id = w.id AND wm.user_id = u.id AND wm.role = 'owner'
                LIMIT 1
            )
            WHERE workspace_id IS NULL
            """
        )
    )
    conn.execute(
        text(
            """
            UPDATE projects SET workspace_id = (SELECT id FROM workspaces ORDER BY id LIMIT 1)
            WHERE workspace_id IS NULL
            AND EXISTS (SELECT 1 FROM workspaces LIMIT 1)
            """
        )
    )

    rows = conn.execute(
        text(
            """
            SELECT pc.user_id, p.workspace_id
            FROM project_collaborators pc
            JOIN projects p ON p.id = pc.project_id
            WHERE p.workspace_id IS NOT NULL
            """
        )
    ).fetchall()

    for user_id, ws_id in rows:
        if not user_id or not ws_id:
            continue
        exists = conn.execute(
            text(
                "SELECT 1 FROM workspace_members WHERE workspace_id = :ws AND user_id = :u LIMIT 1"
            ),
            {"ws": ws_id, "u": user_id},
        ).fetchone()
        if exists:
            continue
        conn.execute(
            text(
                """
                INSERT INTO workspace_members (workspace_id, user_id, role, created_at)
                VALUES (:ws, :u, 'editor', NOW())
                """
            ),
            {"ws": ws_id, "u": user_id},
        )

    for key, name, definition in SYSTEM_TEMPLATES:
        conn.execute(
            text(
                """
                INSERT INTO project_templates (workspace_id, template_key, name, definition, created_at)
                VALUES (NULL, :k, :n, CAST(:def AS jsonb), NOW())
                """
            ),
            {"k": key, "n": name, "def": json.dumps(definition)},
        )

    conn.execute(
        text("UPDATE comments SET visibility = 'author_only' WHERE is_private IS TRUE")
    )

    conn.execute(
        text(
            """
            ALTER TABLE projects
            ALTER COLUMN workspace_id SET NOT NULL
            """
        )
    )


def downgrade() -> None:
    op.drop_index("ix_comments_visibility", table_name="comments")
    op.drop_column("comments", "due_at")
    op.drop_column("comments", "visibility")

    op.drop_index("ix_projects_workspace_id", table_name="projects")
    op.drop_column("projects", "created_from_template_id")
    op.drop_column("projects", "workspace_id")

    op.drop_index("ix_workspace_assets_category", table_name="workspace_assets")
    op.drop_index("ix_workspace_assets_workspace_id", table_name="workspace_assets")
    op.drop_table("workspace_assets")

    op.drop_index("uq_project_templates_system_key", table_name="project_templates")
    op.drop_index("ix_project_templates_template_key", table_name="project_templates")
    op.drop_index("ix_project_templates_workspace_id", table_name="project_templates")
    op.drop_table("project_templates")

    op.drop_index("ix_workspace_invites_token", table_name="workspace_invites")
    op.drop_index("ix_workspace_invites_email", table_name="workspace_invites")
    op.drop_index("ix_workspace_invites_workspace_id", table_name="workspace_invites")
    op.drop_table("workspace_invites")

    op.drop_index("ix_workspace_brandings_custom_domain", table_name="workspace_brandings")
    op.drop_index("ix_workspace_brandings_workspace_id", table_name="workspace_brandings")
    op.drop_table("workspace_brandings")

    op.drop_index("ix_workspace_members_user_id", table_name="workspace_members")
    op.drop_index("ix_workspace_members_workspace_id", table_name="workspace_members")
    op.drop_table("workspace_members")

    op.drop_index("ix_workspaces_owner_user_id", table_name="workspaces")
    op.drop_index("ix_workspaces_slug", table_name="workspaces")
    op.drop_table("workspaces")
