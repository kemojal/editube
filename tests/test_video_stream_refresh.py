"""Task D4: POST /videos/{video_id}/stream/refresh — re-resolve an expired
YouTube googlevideo stream URL (Video.file_path) via yt-dlp, using the
canonical Video.ingest_page_url.

Covers:
- Happy path: expired/malformed `expire` param -> re-resolves and updates
  file_path.
- Rate guard: still-fresh `expire` param (>10min out) -> returns current
  file_path WITHOUT calling the resolver.
- Non-YouTube video (no ingest_page_url) -> 409.
- Resolver failure (YoutubeStreamResolveError) -> 422.
- Not authorized -> 403.

Also covers the guest review-link media proxy (`proxy_review_media` in
`app/services/review_media.py`), which shares the same <10min-freshness
guard to opportunistically refresh a stale googlevideo URL before proxying.
"""

from __future__ import annotations

import time
import unittest
from unittest import mock

from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker

from app.db.database import Base
from app.db.models import Project, User, Video
from app.services.youtube_stream_resolve import YoutubeStreamResolveError


@compiles(JSONB, "sqlite")
def _compile_jsonb_for_sqlite(type_, compiler, **kw):
    # Tests run against in-memory SQLite; JSONB columns degrade to plain JSON.
    return "JSON"


class _SqliteDbTestCase(unittest.TestCase):
    tables = [User.__table__, Project.__table__, Video.__table__]

    def setUp(self) -> None:
        engine = create_engine("sqlite://")
        Base.metadata.create_all(engine, tables=self.tables)
        self.db = sessionmaker(bind=engine)()

        self.user = User(email="editor@example.com", name="Edna Editor", role="creator")
        self.db.add(self.user)
        self.db.flush()

        self.project = Project(name="Launch", creator_id=self.user.id, workspace_id=1)
        self.db.add(self.project)
        self.db.flush()

    def tearDown(self) -> None:
        self.db.close()

    def _make_video(self, *, file_path: str, ingest_page_url: str | None) -> Video:
        video = Video(
            project_id=self.project.id,
            name="Source",
            version=1,
            file_path=file_path,
            ingest_page_url=ingest_page_url,
            uploader_id=self.user.id,
        )
        self.db.add(video)
        self.db.flush()
        self.db.commit()
        return video


class StreamRefreshRouteTests(_SqliteDbTestCase):
    def _call(self, video_id: int):
        from app.api.routes import video_detail as video_detail_routes

        return video_detail_routes.refresh_video_stream(
            video_id=video_id, db=self.db, current_user=self.user
        )

    def test_expired_url_is_reresolved_and_persisted(self):
        from app.api.routes import video_detail as video_detail_routes

        expired = int(time.time()) - 3600
        video = self._make_video(
            file_path=f"https://rr1.googlevideo.com/videoplayback?expire={expired}&id=abc",
            ingest_page_url="https://www.youtube.com/watch?v=abc123XYZ",
        )

        with mock.patch.object(
            video_detail_routes,
            "resolve_youtube_page_to_stream_url",
            return_value="https://rr2.googlevideo.com/videoplayback?expire=9999999999&id=fresh",
        ) as resolve_mock:
            result = self._call(video.id)

        resolve_mock.assert_called_once_with("https://www.youtube.com/watch?v=abc123XYZ")
        self.assertEqual(result["video_id"], video.id)
        self.assertEqual(
            result["file_path"],
            "https://rr2.googlevideo.com/videoplayback?expire=9999999999&id=fresh",
        )

        self.db.refresh(video)
        self.assertEqual(
            video.file_path,
            "https://rr2.googlevideo.com/videoplayback?expire=9999999999&id=fresh",
        )

    def test_malformed_expire_param_is_reresolved(self):
        from app.api.routes import video_detail as video_detail_routes

        video = self._make_video(
            file_path="https://rr1.googlevideo.com/videoplayback?expire=not-a-number&id=abc",
            ingest_page_url="https://www.youtube.com/watch?v=abc123XYZ",
        )

        with mock.patch.object(
            video_detail_routes,
            "resolve_youtube_page_to_stream_url",
            return_value="https://rr2.googlevideo.com/videoplayback?expire=9999999999&id=fresh",
        ) as resolve_mock:
            self._call(video.id)

        resolve_mock.assert_called_once()

    def test_still_fresh_url_short_circuits_without_resolving(self):
        from app.api.routes import video_detail as video_detail_routes

        future = int(time.time()) + 3600  # well beyond the 10min guard window
        stale_but_fresh_url = f"https://rr1.googlevideo.com/videoplayback?expire={future}&id=abc"
        video = self._make_video(
            file_path=stale_but_fresh_url,
            ingest_page_url="https://www.youtube.com/watch?v=abc123XYZ",
        )

        with mock.patch.object(
            video_detail_routes, "resolve_youtube_page_to_stream_url"
        ) as resolve_mock:
            result = self._call(video.id)

        resolve_mock.assert_not_called()
        self.assertEqual(result["file_path"], stale_but_fresh_url)
        self.assertEqual(result["video_id"], video.id)

    def test_non_youtube_video_returns_409(self):
        video = self._make_video(
            file_path="https://cdn.test/uploaded.mp4",
            ingest_page_url=None,
        )

        with self.assertRaises(HTTPException) as ctx:
            self._call(video.id)

        self.assertEqual(ctx.exception.status_code, 409)

    def test_resolve_failure_returns_422(self):
        from app.api.routes import video_detail as video_detail_routes

        expired = int(time.time()) - 3600
        video = self._make_video(
            file_path=f"https://rr1.googlevideo.com/videoplayback?expire={expired}&id=abc",
            ingest_page_url="https://www.youtube.com/watch?v=abc123XYZ",
        )

        with mock.patch.object(
            video_detail_routes,
            "resolve_youtube_page_to_stream_url",
            side_effect=YoutubeStreamResolveError("yt-dlp could not resolve a stream"),
        ):
            with self.assertRaises(HTTPException) as ctx:
                self._call(video.id)

        self.assertEqual(ctx.exception.status_code, 422)

    def test_not_authorized_returns_403(self):
        from app.api.routes import video_detail as video_detail_routes

        other_user = User(email="stranger@example.com", name="Stranger", role="creator")
        self.db.add(other_user)
        self.db.commit()

        video = self._make_video(
            file_path="https://rr1.googlevideo.com/videoplayback?expire=1&id=abc",
            ingest_page_url="https://www.youtube.com/watch?v=abc123XYZ",
        )

        with mock.patch.object(
            video_detail_routes, "can_access_project", return_value=False
        ):
            with self.assertRaises(HTTPException) as ctx:
                video_detail_routes.refresh_video_stream(
                    video_id=video.id, db=self.db, current_user=other_user
                )

        self.assertEqual(ctx.exception.status_code, 403)

    def test_missing_video_returns_404(self):
        with self.assertRaises(HTTPException) as ctx:
            self._call(999999)

        self.assertEqual(ctx.exception.status_code, 404)


class StreamUrlExpireParseTests(unittest.TestCase):
    def test_parses_valid_expire(self):
        from app.api.routes.video_detail import _stream_url_expire_at

        self.assertEqual(
            _stream_url_expire_at("https://rr1.googlevideo.com/videoplayback?expire=1700000000&id=x"),
            1700000000,
        )

    def test_missing_expire_returns_none(self):
        from app.api.routes.video_detail import _stream_url_expire_at

        self.assertIsNone(_stream_url_expire_at("https://cdn.test/plain.mp4"))
        self.assertIsNone(_stream_url_expire_at(None))
        self.assertIsNone(_stream_url_expire_at(""))

    def test_malformed_expire_returns_none(self):
        from app.api.routes.video_detail import _stream_url_expire_at

        self.assertIsNone(
            _stream_url_expire_at("https://rr1.googlevideo.com/videoplayback?expire=abc&id=x")
        )


class _FakeUpstreamResponse:
    def __init__(self, status_code: int = 200, headers: dict | None = None):
        self.status_code = status_code
        self.headers = headers if headers is not None else {"content-type": "video/mp4"}
        self.aclose_called = False

    async def aiter_bytes(self, chunk_size: int = 262_144):
        for chunk in ():
            yield chunk  # pragma: no cover - intentionally empty

    async def aclose(self) -> None:
        self.aclose_called = True


class _FakeAsyncClient:
    """Stand-in for httpx.AsyncClient that records the URL it was asked to fetch."""

    instances: list["_FakeAsyncClient"] = []

    def __init__(self, *args, **kwargs):
        self.sent_url: str | None = None
        self.aclose_called = False
        _FakeAsyncClient.instances.append(self)

    def build_request(self, method: str, url: str, headers: dict | None = None):
        self.sent_url = url
        return mock.Mock(method=method, url=url, headers=headers or {})

    async def send(self, request, stream: bool = True):
        return _FakeUpstreamResponse()

    async def aclose(self) -> None:
        self.aclose_called = True


class _FakeRequest:
    """Minimal stand-in for fastapi.Request — proxy_review_media only reads headers."""

    def __init__(self, headers: dict | None = None):
        self.headers = headers or {}


class ReviewMediaProxyRefreshTests(_SqliteDbTestCase):
    """proxy_review_media (app/services/review_media.py) opportunistically
    re-resolves an expired/expiring googlevideo URL before proxying, sharing
    the same freshness guard as the D4 stream-refresh endpoint."""

    def setUp(self) -> None:
        super().setUp()
        _FakeAsyncClient.instances = []

    def _run(self, coro):
        import asyncio

        return asyncio.run(coro)

    def test_proxy_refreshes_when_expired(self):
        from app.services import review_media

        expired = int(time.time()) - 3600
        video = self._make_video(
            file_path=f"https://rr1.googlevideo.com/videoplayback?expire={expired}&id=abc",
            ingest_page_url="https://www.youtube.com/watch?v=abc123XYZ",
        )

        with mock.patch.object(
            review_media,
            "resolve_youtube_page_to_stream_url",
            return_value="https://rr2.googlevideo.com/videoplayback?expire=9999999999&id=fresh",
        ) as resolve_mock, mock.patch.object(
            review_media.httpx, "AsyncClient", _FakeAsyncClient
        ):
            self._run(
                review_media.proxy_review_media(
                    request=_FakeRequest(),
                    video=video,
                    purpose="playback",
                    db=self.db,
                )
            )

        resolve_mock.assert_called_once_with("https://www.youtube.com/watch?v=abc123XYZ")
        self.assertEqual(len(_FakeAsyncClient.instances), 1)
        self.assertEqual(
            _FakeAsyncClient.instances[0].sent_url,
            "https://rr2.googlevideo.com/videoplayback?expire=9999999999&id=fresh",
        )

        self.db.refresh(video)
        self.assertEqual(
            video.file_path,
            "https://rr2.googlevideo.com/videoplayback?expire=9999999999&id=fresh",
        )

    def test_proxy_short_circuits_when_fresh(self):
        from app.services import review_media

        future = int(time.time()) + 3600  # well beyond the 10min guard window
        fresh_url = f"https://rr1.googlevideo.com/videoplayback?expire={future}&id=abc"
        video = self._make_video(
            file_path=fresh_url,
            ingest_page_url="https://www.youtube.com/watch?v=abc123XYZ",
        )

        with mock.patch.object(
            review_media, "resolve_youtube_page_to_stream_url"
        ) as resolve_mock, mock.patch.object(
            review_media.httpx, "AsyncClient", _FakeAsyncClient
        ):
            self._run(
                review_media.proxy_review_media(
                    request=_FakeRequest(),
                    video=video,
                    purpose="playback",
                    db=self.db,
                )
            )

        resolve_mock.assert_not_called()
        self.assertEqual(_FakeAsyncClient.instances[0].sent_url, fresh_url)
        self.db.refresh(video)
        self.assertEqual(video.file_path, fresh_url)

    def test_proxy_non_ingest_video_untouched(self):
        from app.services import review_media

        plain_url = "https://cdn.test/uploaded.mp4"
        video = self._make_video(file_path=plain_url, ingest_page_url=None)

        with mock.patch.object(
            review_media, "resolve_youtube_page_to_stream_url"
        ) as resolve_mock, mock.patch.object(
            review_media.httpx, "AsyncClient", _FakeAsyncClient
        ):
            self._run(
                review_media.proxy_review_media(
                    request=_FakeRequest(),
                    video=video,
                    purpose="playback",
                    db=self.db,
                )
            )

        resolve_mock.assert_not_called()
        self.assertEqual(_FakeAsyncClient.instances[0].sent_url, plain_url)
        self.db.refresh(video)
        self.assertEqual(video.file_path, plain_url)

    def test_proxy_resolver_failure_falls_back_to_stale(self):
        from app.services import review_media

        expired = int(time.time()) - 3600
        stale_url = f"https://rr1.googlevideo.com/videoplayback?expire={expired}&id=abc"
        video = self._make_video(
            file_path=stale_url,
            ingest_page_url="https://www.youtube.com/watch?v=abc123XYZ",
        )

        with mock.patch.object(
            review_media,
            "resolve_youtube_page_to_stream_url",
            side_effect=YoutubeStreamResolveError("yt-dlp could not resolve a stream"),
        ) as resolve_mock, mock.patch.object(
            review_media.httpx, "AsyncClient", _FakeAsyncClient
        ):
            # Must not raise — proxy falls through to the stale URL.
            self._run(
                review_media.proxy_review_media(
                    request=_FakeRequest(),
                    video=video,
                    purpose="playback",
                    db=self.db,
                )
            )

        resolve_mock.assert_called_once()
        self.assertEqual(_FakeAsyncClient.instances[0].sent_url, stale_url)
        self.db.refresh(video)
        self.assertEqual(video.file_path, stale_url)


if __name__ == "__main__":
    unittest.main()
