"""A YouTube-sourced video's `file_path` must be playable in a browser.

yt-dlp's default format selection is `bestvideo*+bestaudio`, so `yt-dlp -g`
prints TWO urls — a video-only stream and an audio-only stream — and taking the
first one stores a **video-only** DASH stream (recently itag 401: AV1, 2160p) as
the video's playback url. It fetches fine (206, video/mp4), which is why nothing
errors; it just renders a black viewer with a running clock and no sound.

The playback url therefore has to be an explicitly *muxed* progressive stream
(avc1 + aac, i.e. itag 18/22), and a stored url that isn't one has to be
re-resolved the same way an expired one is.

Audio-only resolution for transcription (`resolve_youtube_page_to_audio_stream_url`)
is deliberately unaffected.
"""

from __future__ import annotations

import time
import unittest
from unittest import mock

from app.services.youtube_stream_resolve import (
    is_stream_url_muxed,
    resolve_youtube_page_to_stream_url,
    should_refresh_stream_url,
    stream_url_itag,
)

VIDEO_ONLY_AV1 = (
    "https://rr4---sn-ipoxu.googlevideo.com/videoplayback?expire=9999999999"
    "&itag=401&source=youtube&mime=video%2Fmp4&gir=yes"
)
MUXED_360P = (
    "https://rr4---sn-ipoxu.googlevideo.com/videoplayback?expire=9999999999"
    "&itag=18&source=youtube&mime=video%2Fmp4"
)
MUXED_720P = (
    "https://rr4---sn-ipoxu.googlevideo.com/videoplayback?expire=9999999999&itag=22"
)


class StreamUrlItagTests(unittest.TestCase):
    def test_reads_the_itag(self):
        self.assertEqual(stream_url_itag(VIDEO_ONLY_AV1), 401)
        self.assertEqual(stream_url_itag(MUXED_360P), 18)

    def test_unknown_when_absent_or_malformed(self):
        self.assertIsNone(stream_url_itag(None))
        self.assertIsNone(stream_url_itag("https://example.com/clip.mp4"))
        self.assertIsNone(stream_url_itag("https://x.googlevideo.com/videoplayback?itag=abc"))

    def test_recognises_muxed_progressive_streams(self):
        self.assertTrue(is_stream_url_muxed(MUXED_360P))
        self.assertTrue(is_stream_url_muxed(MUXED_720P))

    def test_rejects_video_only_dash_streams(self):
        self.assertFalse(is_stream_url_muxed(VIDEO_ONLY_AV1))
        for itag in (137, 136, 135, 134, 133, 160, 399, 400, 401, 140, 251):
            url = f"https://x.googlevideo.com/videoplayback?itag={itag}"
            self.assertFalse(is_stream_url_muxed(url), f"itag {itag} is not muxed")

    def test_a_file_upload_path_is_left_alone(self):
        # Not a googlevideo url at all: uploads carry no itag and must never be
        # judged unplayable by this heuristic.
        self.assertTrue(is_stream_url_muxed("/uploads/videos/take-1.mp4"))
        self.assertTrue(is_stream_url_muxed("https://res.cloudinary.com/x/take-1.mp4"))


class ShouldRefreshStreamUrlTests(unittest.TestCase):
    def test_refreshes_a_video_only_stream_even_when_it_is_fresh(self):
        fresh_video_only = (
            "https://x.googlevideo.com/videoplayback?itag=401&expire="
            f"{int(time.time()) + 6 * 3600}"
        )
        self.assertTrue(should_refresh_stream_url(fresh_video_only))

    def test_leaves_a_fresh_muxed_stream_alone(self):
        fresh_muxed = (
            f"https://x.googlevideo.com/videoplayback?itag=18&expire={int(time.time()) + 6 * 3600}"
        )
        self.assertFalse(should_refresh_stream_url(fresh_muxed))

    def test_refreshes_an_expiring_muxed_stream(self):
        expiring = (
            f"https://x.googlevideo.com/videoplayback?itag=18&expire={int(time.time()) + 60}"
        )
        self.assertTrue(should_refresh_stream_url(expiring))

    def test_refreshes_when_expiry_is_unknown(self):
        self.assertTrue(should_refresh_stream_url("https://x.googlevideo.com/videoplayback?itag=18"))
        self.assertTrue(should_refresh_stream_url(None))


class ResolvePlayableStreamTests(unittest.TestCase):
    def test_asks_yt_dlp_for_a_muxed_stream_rather_than_bestvideo(self):
        with mock.patch(
            "app.services.youtube_stream_resolve._yt_dlp_g_lines",
            return_value=[MUXED_360P],
        ) as lines:
            self.assertEqual(
                resolve_youtube_page_to_stream_url("https://www.youtube.com/watch?v=x"),
                MUXED_360P,
            )
        # A format selector was passed, and it constrains audio + video.
        args = lines.call_args.args
        self.assertEqual(args[0], "https://www.youtube.com/watch?v=x")
        self.assertIn("-f", args[1:])
        selector = args[args.index("-f", 1) + 1]
        self.assertIn("acodec!=none", selector)
        # Video is constrained too — either "present" or pinned to a codec.
        self.assertTrue(
            "vcodec!=none" in selector or "vcodec^=" in selector,
            f"selector does not constrain video: {selector}",
        )

    def test_falls_through_selectors_until_one_resolves(self):
        from app.services.youtube_stream_resolve import YoutubeStreamResolveError

        calls: list[tuple] = []

        def fake(url, *format_args):
            calls.append(format_args)
            if len(calls) < 3:
                raise YoutubeStreamResolveError("Requested format is not available")
            return [MUXED_720P]

        with mock.patch("app.services.youtube_stream_resolve._yt_dlp_g_lines", side_effect=fake):
            self.assertEqual(
                resolve_youtube_page_to_stream_url("https://www.youtube.com/watch?v=x"),
                MUXED_720P,
            )
        self.assertEqual(len(calls), 3)

    def test_never_returns_a_video_only_stream_when_a_muxed_one_resolves(self):
        def fake(url, *format_args):
            # Whatever is asked for, yt-dlp here only ever has the muxed one.
            return [MUXED_360P]

        with mock.patch("app.services.youtube_stream_resolve._yt_dlp_g_lines", side_effect=fake):
            resolved = resolve_youtube_page_to_stream_url("https://www.youtube.com/watch?v=x")
        self.assertTrue(is_stream_url_muxed(resolved))

    def test_last_resort_takes_the_first_default_line_so_playback_is_never_urlless(self):
        from app.services.youtube_stream_resolve import YoutubeStreamResolveError

        def fake(url, *format_args):
            if format_args:
                raise YoutubeStreamResolveError("Requested format is not available")
            return [VIDEO_ONLY_AV1, "https://x.googlevideo.com/videoplayback?itag=140"]

        with mock.patch("app.services.youtube_stream_resolve._yt_dlp_g_lines", side_effect=fake):
            self.assertEqual(
                resolve_youtube_page_to_stream_url("https://www.youtube.com/watch?v=x"),
                VIDEO_ONLY_AV1,
            )

    def test_propagates_failure_when_nothing_resolves(self):
        from app.services.youtube_stream_resolve import YoutubeStreamResolveError

        with mock.patch(
            "app.services.youtube_stream_resolve._yt_dlp_g_lines",
            side_effect=YoutubeStreamResolveError("Video unavailable"),
        ):
            with self.assertRaises(YoutubeStreamResolveError):
                resolve_youtube_page_to_stream_url("https://www.youtube.com/watch?v=x")


class BestVideoResolverTests(unittest.TestCase):
    """The ffmpeg pipelines keep the old behaviour: resolution over muxing."""

    def test_takes_yt_dlps_default_first_line_with_no_selector(self):
        from app.services.youtube_stream_resolve import (
            resolve_youtube_page_to_best_video_stream_url,
        )

        with mock.patch(
            "app.services.youtube_stream_resolve._yt_dlp_g_lines",
            return_value=[VIDEO_ONLY_AV1, "https://x.googlevideo.com/videoplayback?itag=140"],
        ) as lines:
            self.assertEqual(
                resolve_youtube_page_to_best_video_stream_url("https://www.youtube.com/watch?v=x"),
                VIDEO_ONLY_AV1,
            )
        lines.assert_called_once_with("https://www.youtube.com/watch?v=x")


class RenderSourceDoesNotBreakPlaybackTests(unittest.TestCase):
    """
    A clip render refreshes the source URL and used to write it straight back to
    `video.file_path` — so a render turned the viewer black, because the URL it
    wants is video-only. It may still use that URL; it may not store it.
    """

    def setUp(self) -> None:
        from app.services import clip_renderer

        self.clip_renderer = clip_renderer

    def _video(self):
        class _V:
            id = 7
            file_path = MUXED_360P
            ingest_page_url = "https://www.youtube.com/watch?v=x"

        return _V()

    def test_uses_the_video_only_url_without_persisting_it(self):
        video = self._video()
        db = mock.MagicMock()
        with mock.patch.object(
            self.clip_renderer,
            "resolve_youtube_page_to_best_video_stream_url",
            return_value=VIDEO_ONLY_AV1,
        ):
            resolved = self.clip_renderer._resolve_video_source_url(db, video)

        self.assertEqual(resolved, VIDEO_ONLY_AV1)
        self.assertEqual(video.file_path, MUXED_360P, "playback URL must survive a render")
        db.commit.assert_not_called()

    def test_still_persists_a_muxed_refresh(self):
        video = self._video()
        video.file_path = "https://x.googlevideo.com/videoplayback?itag=18&expire=1"
        db = mock.MagicMock()
        with mock.patch.object(
            self.clip_renderer,
            "resolve_youtube_page_to_best_video_stream_url",
            return_value=MUXED_720P,
        ):
            resolved = self.clip_renderer._resolve_video_source_url(db, video)

        self.assertEqual(resolved, MUXED_720P)
        self.assertEqual(video.file_path, MUXED_720P)
        db.commit.assert_called_once()


if __name__ == "__main__":
    unittest.main()
