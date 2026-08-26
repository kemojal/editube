"""`GET .../videos/{id}/next-version-preview`.

Backs the promise the upload dialog makes before an editor commits: how many
open change requests will move, and what the new version will be called.
"""

from __future__ import annotations

import pytest


@pytest.fixture
def ctx(db_session, make_user, make_project, make_video):
    owner = make_user(name="Ollie Owner")
    project = make_project(creator=owner, name="Launch")
    video = make_video(project, name="Hero Cut", version=2, uploader_id=owner.id)
    db_session.commit()
    return {"owner": owner, "project": project, "video": video}


class NextVersionPreviewTests:
    def _get(self, api_client, ctx):
        return api_client.get(
            f"/projects/{ctx['project'].id}/videos/{ctx['video'].id}/next-version-preview"
        )

    def test_reports_the_next_ordinal(self, api_client, ctx) -> None:
        api_client.login(ctx["owner"])

        body = self._get(api_client, ctx).json()

        assert body["current_version"] == 2
        assert body["next_version"] == 3
        assert body["suggested_name"] == "Hero Cut"

    def test_counts_only_what_will_actually_move(
        self, api_client, db_session, ctx, make_comment
    ) -> None:
        video = ctx["video"]
        make_comment(video, kind="change_request", status="open")
        make_comment(video, kind="change_request", status="in_progress")
        make_comment(video, kind="change_request", status="resolved")
        make_comment(video, kind="comment", status="open")
        db_session.commit()
        api_client.login(ctx["owner"])

        assert self._get(api_client, ctx).json()["carry_forward_count"] == 2

    def test_zero_when_nothing_is_open(self, api_client, ctx) -> None:
        api_client.login(ctx["owner"])
        assert self._get(api_client, ctx).json()["carry_forward_count"] == 0

    def test_requires_authentication(self, api_client, ctx) -> None:
        assert self._get(api_client, ctx).status_code == 401

    def test_missing_video_is_a_404(self, api_client, ctx) -> None:
        api_client.login(ctx["owner"])
        response = api_client.get(
            f"/projects/{ctx['project'].id}/videos/999999/next-version-preview"
        )
        assert response.status_code == 404
