"""The assets library — one list for everything a workspace holds.

Library uploads (`workspace_assets`) and project cuts (`videos`) were stored in
different tables with no combined view, so "show me everything I've uploaded"
had no answer. These tests pin the merged feed, the filters the page offers,
and the two things the merge is easy to get wrong: who may read it, and
whether library bytes count against the plan's storage cap.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from app.db.models import Workspace, WorkspaceAsset, WorkspaceMember
from app.services.storage_policy import get_workspace_storage_snapshot


@pytest.fixture
def ctx(db_session, make_user, make_project):
    owner = make_user(name="Ollie Owner", plan="pro")
    outsider = make_user(name="Otto Outsider")
    workspace = Workspace(name="Studio", slug="studio", owner_user_id=owner.id)
    db_session.add(workspace)
    db_session.flush()
    db_session.add(
        WorkspaceMember(workspace_id=workspace.id, user_id=owner.id, role="owner")
    )
    project = make_project(creator=owner, name="Launch", workspace_id=workspace.id)
    db_session.commit()
    return {
        "owner": owner,
        "outsider": outsider,
        "workspace": workspace,
        "project": project,
    }


def _add_asset(db_session, workspace, **overrides):
    asset = WorkspaceAsset(
        workspace_id=workspace.id,
        category=overrides.pop("category", "music"),
        title=overrides.pop("title", "Theme"),
        file_url=overrides.pop("file_url", "https://cdn.example.test/theme.mp3"),
        mime_type=overrides.pop("mime_type", "audio/mpeg"),
        size_bytes=overrides.pop("size_bytes", 1_000),
        created_at=overrides.pop("created_at", datetime(2026, 8, 1, 12, 0, 0)),
        **overrides,
    )
    db_session.add(asset)
    db_session.commit()
    return asset


def _feed(api_client, user, workspace, **params):
    api_client.login(user)
    response = api_client.get(f"/workspaces/{workspace.id}/library", params=params)
    assert response.status_code == 200, response.text
    return response.json()


class FeedTests:
    def test_library_assets_and_project_cuts_arrive_in_one_list(
        self, api_client, db_session, ctx, make_video
    ) -> None:
        _add_asset(db_session, ctx["workspace"], title="Theme")
        make_video(ctx["project"], name="Hero Cut")
        db_session.commit()

        feed = _feed(api_client, ctx["owner"], ctx["workspace"])

        assert {item["title"] for item in feed["items"]} == {"Theme", "Hero Cut"}
        assert {item["source"] for item in feed["items"]} == {"library", "project"}
        assert feed["total"] == 2

    def test_newest_first_across_both_sources(
        self, api_client, db_session, ctx, make_video
    ) -> None:
        _add_asset(
            db_session,
            ctx["workspace"],
            title="Old Logo",
            created_at=datetime(2026, 1, 1, 9, 0, 0),
        )
        video = make_video(ctx["project"], name="Fresh Cut")
        video.created_at = datetime(2026, 8, 20, 9, 0, 0)
        db_session.commit()

        feed = _feed(api_client, ctx["owner"], ctx["workspace"])

        assert [item["title"] for item in feed["items"]] == ["Fresh Cut", "Old Logo"]

    def test_kind_filter_narrows_to_one_media_type(
        self, api_client, db_session, ctx, make_video
    ) -> None:
        _add_asset(db_session, ctx["workspace"], title="Theme", mime_type="audio/mpeg")
        make_video(ctx["project"], name="Hero Cut")
        db_session.commit()

        feed = _feed(api_client, ctx["owner"], ctx["workspace"], kind="audio")

        assert [item["title"] for item in feed["items"]] == ["Theme"]
        # The counts describe the whole workspace, not the filtered slice —
        # otherwise every tab would read "0" for the tabs you are not on.
        assert feed["counts"]["video"] == 1
        assert feed["counts"]["audio"] == 1

    def test_search_matches_titles_from_either_source(
        self, api_client, db_session, ctx, make_video
    ) -> None:
        _add_asset(db_session, ctx["workspace"], title="Sunset b-roll")
        make_video(ctx["project"], name="Sunset cut")
        make_video(ctx["project"], name="Studio interview")
        db_session.commit()

        feed = _feed(api_client, ctx["owner"], ctx["workspace"], q="sunset")

        assert {item["title"] for item in feed["items"]} == {"Sunset b-roll", "Sunset cut"}

    def test_project_cuts_are_not_deletable_from_the_library(
        self, api_client, db_session, ctx, make_video
    ) -> None:
        _add_asset(db_session, ctx["workspace"], title="Theme")
        make_video(ctx["project"], name="Hero Cut")
        db_session.commit()

        feed = _feed(api_client, ctx["owner"], ctx["workspace"])
        by_title = {item["title"]: item for item in feed["items"]}

        # Deleting a cut supersedes versions and sign-offs; that decision lives
        # in the project, not in a file browser.
        assert by_title["Hero Cut"]["can_delete"] is False
        assert by_title["Theme"]["can_delete"] is True

    def test_a_cut_carries_the_project_it_belongs_to(
        self, api_client, db_session, ctx, make_video
    ) -> None:
        make_video(ctx["project"], name="Hero Cut")
        db_session.commit()

        feed = _feed(api_client, ctx["owner"], ctx["workspace"])

        assert feed["items"][0]["project"] == {
            "id": ctx["project"].id,
            "name": "Launch",
        }

    def test_non_members_cannot_read_the_library(
        self, api_client, db_session, ctx
    ) -> None:
        _add_asset(db_session, ctx["workspace"], title="Theme")

        api_client.login(ctx["outsider"])
        response = api_client.get(f"/workspaces/{ctx['workspace'].id}/library")

        assert response.status_code == 403


class PermissionTests:
    def test_a_client_sees_the_library_but_cannot_delete_from_it(
        self, api_client, db_session, ctx, make_user
    ) -> None:
        client = make_user(name="Cleo Client")
        db_session.add(
            WorkspaceMember(
                workspace_id=ctx["workspace"].id, user_id=client.id, role="client"
            )
        )
        _add_asset(db_session, ctx["workspace"], title="Theme")

        feed = _feed(api_client, client, ctx["workspace"])

        # The write routes refuse clients, so a Delete offered here would only
        # ever produce a 403 the user could not have predicted.
        assert [item["can_delete"] for item in feed["items"]] == [False]


class PaginationTests:
    def test_offset_walks_the_merged_list(
        self, api_client, db_session, ctx
    ) -> None:
        base = datetime(2026, 8, 1, 12, 0, 0)
        for n in range(5):
            _add_asset(
                db_session,
                ctx["workspace"],
                title=f"Asset {n}",
                created_at=base + timedelta(hours=n),
            )

        page = _feed(api_client, ctx["owner"], ctx["workspace"], limit=2, offset=2)

        assert [item["title"] for item in page["items"]] == ["Asset 2", "Asset 1"]
        assert page["total"] == 5


class StorageAccountingTests:
    def test_library_bytes_count_against_the_plan_cap(
        self, db_session, ctx, make_video
    ) -> None:
        make_video(ctx["project"], name="Hero Cut", size_bytes=2_000)
        _add_asset(db_session, ctx["workspace"], size_bytes=3_000)

        snapshot = get_workspace_storage_snapshot(
            db_session, user=ctx["owner"], workspace_id=ctx["workspace"].id
        )

        # 3 GB of music used to read as 0 B used: the meter summed videos only.
        assert snapshot.used_bytes == 5_000


class UploadValidationTests:
    def test_an_unknown_category_is_refused(self, api_client, ctx) -> None:
        api_client.login(ctx["owner"])

        response = api_client.post(
            f"/workspaces/{ctx['workspace'].id}/assets",
            data={"title": "Theme", "category": "not-a-category"},
            files={"file": ("theme.mp3", b"id3", "audio/mpeg")},
        )

        assert response.status_code == 400
        assert "allowed" in response.json()["detail"]
