"""Resumable multipart upload endpoints.

The single-PUT path dies at 5 GB and restarts from zero on any hiccup; its 413
message referenced a multipart flow that did not exist. These pin the flow that
now does — against a fake backend, since the suite must never touch R2.
"""

from __future__ import annotations

import pytest


class FakeBackend:
    def __init__(self) -> None:
        self.aborted: list[tuple[str, str]] = []
        self.completed: list[dict] = []

    def available(self) -> bool:
        return True

    def create_multipart_upload(self, *, key: str, content_type: str) -> str:
        return "upload-123"

    def presign_part_urls(self, *, key: str, upload_id: str, part_count: int, expires_in: int = 0):
        return [f"https://r2.example.test/{key}?part={n}" for n in range(1, part_count + 1)]

    def complete_multipart_upload(self, *, key: str, upload_id: str, parts: list[dict]) -> str:
        self.completed.append({"key": key, "upload_id": upload_id, "parts": parts})
        return f"https://cdn.example.test/{key}"

    def abort_multipart_upload(self, *, key: str, upload_id: str) -> None:
        self.aborted.append((key, upload_id))

    def public_url(self, key: str) -> str:
        return f"https://cdn.example.test/{key}"


@pytest.fixture
def fake_backend(monkeypatch):
    backend = FakeBackend()
    import app.api.routes.upload as upload_module

    monkeypatch.setattr(upload_module, "get_storage", lambda: backend)
    monkeypatch.setattr(upload_module, "multipart_supported", lambda: True)
    return backend


class MultipartCreateTests:
    def test_returns_one_url_per_part(self, api_client, db_session, make_user, fake_backend) -> None:
        api_client.login(make_user())
        db_session.commit()

        body = api_client.post(
            "/upload/multipart/create",
            json={"filename": "cut.mp4", "content_type": "video/mp4", "size": 70 * 1024 * 1024},
        ).json()

        # 70 MB at 32 MB parts = 3 parts, and the count must match the URLs.
        assert body["part_count"] == 3
        assert len(body["part_urls"]) == 3
        assert body["upload_id"] == "upload-123"
        assert body["file_path"].startswith("https://cdn.example.test/")

    def test_a_file_over_the_old_5gb_wall_is_accepted(
        self, api_client, db_session, make_user, fake_backend
    ) -> None:
        api_client.login(make_user())
        db_session.commit()

        response = api_client.post(
            "/upload/multipart/create",
            json={"filename": "feature.mp4", "content_type": "video/mp4", "size": 20 * 1024**3},
        )

        assert response.status_code == 200

    def test_the_new_ceiling_still_exists(self, api_client, db_session, make_user, fake_backend) -> None:
        api_client.login(make_user())
        db_session.commit()
        response = api_client.post(
            "/upload/multipart/create",
            json={"filename": "absurd.mp4", "content_type": "video/mp4", "size": 101 * 1024**3},
        )
        assert response.status_code == 413

    def test_unsupported_storage_is_a_501_not_a_500(
        self, api_client, db_session, make_user, monkeypatch
    ) -> None:
        # The client probes this status to fall back to the legacy path;
        # a 500 would read as "broken" instead of "unavailable".
        import app.api.routes.upload as upload_module

        monkeypatch.setattr(upload_module, "multipart_supported", lambda: False)
        api_client.login(make_user())
        db_session.commit()

        response = api_client.post(
            "/upload/multipart/create",
            json={"filename": "cut.mp4", "content_type": "video/mp4", "size": 1024},
        )
        assert response.status_code == 501

    def test_non_video_is_refused(self, api_client, db_session, make_user, fake_backend) -> None:
        api_client.login(make_user())
        db_session.commit()
        response = api_client.post(
            "/upload/multipart/create",
            json={"filename": "cat.png", "content_type": "image/png", "size": 1024},
        )
        assert response.status_code == 415

    def test_requires_authentication(self, api_client, fake_backend) -> None:
        response = api_client.post(
            "/upload/multipart/create",
            json={"filename": "cut.mp4", "content_type": "video/mp4", "size": 1024},
        )
        assert response.status_code == 401


class MultipartCompleteTests:
    def test_forwards_parts_and_returns_the_url(
        self, api_client, db_session, make_user, fake_backend
    ) -> None:
        api_client.login(make_user())
        db_session.commit()

        body = api_client.post(
            "/upload/multipart/complete",
            json={
                "key": "videos/cut.mp4",
                "upload_id": "upload-123",
                "parts": [
                    {"part_number": 2, "etag": "bbb"},
                    {"part_number": 1, "etag": "aaa"},
                ],
            },
        ).json()

        assert body["file_path"] == "https://cdn.example.test/videos/cut.mp4"
        assert fake_backend.completed[0]["parts"][0]["part_number"] == 2  # passthrough

    def test_abort_frees_the_parts(self, api_client, db_session, make_user, fake_backend) -> None:
        api_client.login(make_user())
        db_session.commit()

        response = api_client.post(
            "/upload/multipart/abort",
            json={"key": "videos/cut.mp4", "upload_id": "upload-123"},
        )

        assert response.json()["ok"] is True
        assert fake_backend.aborted == [("videos/cut.mp4", "upload-123")]


class FromUploadVersionOfTests:
    def test_registration_joins_the_version_chain(
        self, api_client, db_session, make_user, make_project, make_video, make_comment, monkeypatch
    ) -> None:
        # The resumable path registers via /from-upload — it must get the same
        # carry-forward and status reset as the classic multipart route.
        owner = make_user()
        project = make_project(creator=owner)
        v1 = make_video(project, version=1, version_group_id="grp-x", uploader_id=owner.id)
        make_comment(v1, kind="change_request", status="open", text="Fix the logo")
        db_session.commit()
        api_client.login(owner)

        body = api_client.post(
            f"/projects/{project.id}/videos/from-upload",
            json={
                "file_path": "https://cdn.example.test/v2.mp4",
                "name": "Hero Cut",
                "version_of": v1.id,
                "version_notes": "Logo fixed, music lowered.",
                "size_bytes": 10,
            },
        ).json()

        assert body["version"] == 2
        assert body["status"] == "in_review"
        assert body["version_notes"] == "Logo fixed, music lowered."

        from app.db.models import Comment

        carried = (
            db_session.query(Comment)
            .filter(Comment.video_id == body["id"], Comment.carried_from_comment_id.isnot(None))
            .count()
        )
        assert carried == 1
