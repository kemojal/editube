"""G7: mask tracking fails fast with a sentence, never an AttributeError.

The installed opencv-python-headless has no CSRT tracker, so the job used to
commit `processing`, open the capture, and then die with a raw AttributeError
the UI showed verbatim. Now availability is probed up front, the row fails
with an actionable reason, and the capability registry reports the same
answer the job would give.
"""

from __future__ import annotations

import pytest

from app.db.models import AiResult
from app.jobs.mask_track import mask_track_job, tracker_availability
from app.jobs.rough_cut_export import _masks_for_segment


class TestTrackerAvailability:
    def test_reports_a_backend_or_an_actionable_reason(self):
        backend, reason = tracker_availability()
        if backend is None:
            assert reason and ("opencv-contrib" in reason or "OpenCV" in reason)
        else:
            assert backend in {"csrt", "mil"} and reason is None

    def test_mil_is_opt_in_not_a_silent_downgrade(self, monkeypatch):
        import app.jobs.mask_track as mask_track_module

        class _NoCSRT:
            @staticmethod
            def TrackerMIL_create():  # pragma: no cover — presence is the point
                return object()

        monkeypatch.setitem(__import__("sys").modules, "cv2", _NoCSRT())
        monkeypatch.delenv("MASK_TRACK_ALLOW_MIL", raising=False)
        backend, reason = mask_track_module.tracker_availability()
        assert backend is None and "MASK_TRACK_ALLOW_MIL" in (reason or "")

        monkeypatch.setenv("MASK_TRACK_ALLOW_MIL", "1")
        backend, reason = mask_track_module.tracker_availability()
        assert backend == "mil" and reason is None


class TestJobFailsFast:
    def test_missing_tracker_fails_the_row_with_the_sentence(
        self, db_session, make_video, monkeypatch
    ):
        video = make_video(duration=10)
        row = AiResult(
            video_id=video.id,
            result_type="mask_track",
            status="queued",
            result_data={"mask": {}, "direction": "both"},
        )
        db_session.add(row)
        db_session.commit()

        monkeypatch.setattr(
            "app.jobs.mask_track.tracker_availability",
            lambda: (None, "This server's OpenCV build has no CSRT tracker …"),
        )
        monkeypatch.setattr("app.jobs.mask_track.SessionLocal", lambda: db_session)
        # Session close would tear down the shared test session.
        monkeypatch.setattr(db_session, "close", lambda: None)

        mask_track_job(row.id)

        db_session.refresh(row)
        assert row.status == "failed"
        assert "CSRT" in (row.error_message or "")
        assert row.result_data["error"] == row.error_message
        # And it failed BEFORE claiming to be processing anything.
        assert row.result_data.get("progress") is None


class TestPerClipMaskScoping:
    MASKS = [
        {"id": "m1", "sourceRange": {"start": 0.0, "end": 5.0}},
        {"id": "m2", "sourceRange": {"start": 5.0, "end": 9.0}},
        {"id": "legacy-unstamped"},
    ]

    def test_each_segment_gets_its_own_masks_plus_legacy_ones(self):
        first = _masks_for_segment(self.MASKS, 0.0, 5.0)
        assert [m["id"] for m in first] == ["m1", "legacy-unstamped"]
        second = _masks_for_segment(self.MASKS, 5.0, 9.0)
        assert [m["id"] for m in second] == ["m2", "legacy-unstamped"]

    def test_the_stamp_is_stripped_before_the_matte_renderer(self):
        [mask, _] = _masks_for_segment(self.MASKS, 0.0, 5.0)
        assert "sourceRange" not in mask

    def test_clamped_last_clip_still_matches_by_edge(self):
        masks = [{"id": "m", "sourceRange": {"start": 5.0, "end": 9.037}}]
        assert _masks_for_segment(masks, 5.0, 9.0) == [{"id": "m"}]

    def test_a_span_sharing_no_edge_does_not_leak_across_clips(self):
        masks = [{"id": "m", "sourceRange": {"start": 2.0, "end": 3.0}}]
        assert _masks_for_segment(masks, 5.0, 9.0) == []
