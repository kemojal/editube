"""The SAM2 propagation tracking backend — the pure core, without torch."""

from __future__ import annotations

import pytest

from app.db.models import AiResult
from app.jobs.mask_track import mask_track_job, tracker_availability
from app.services.propagation_tracker import mask_bboxes_to_keyframes

FPS = 30.0
FRAME = (1000, 500)


def _boxes(n: int, lost_from: int | None = None):
    out = []
    for index in range(n):
        if lost_from is not None and index >= lost_from:
            out.append(None)
        else:
            out.append((100.0 + index, 50.0, 200.0, 100.0))
    return out


class TestMaskBboxesToKeyframes:
    def test_anchor_always_emits_in_clip_relative_time(self):
        keyframes, lost = mask_bboxes_to_keyframes(
            _boxes(5),
            fps=FPS,
            clip_start=2.0,
            anchor_index=0,
            frame_size=FRAME,
            source_frame_offset=60,  # clip_start * fps
        )
        assert lost is None
        first = keyframes[0]
        assert first["frame"] == 60
        assert first["t"] == pytest.approx(0.0)
        assert first["rotation"] == 0.0
        # Transform space is CENTER-offset percent-of-frame (the CSRT path's
        # own convention): box (100,50,200,100) in 1000x500 → centre (200,100)
        # → x = -30% of width from centre, width = 20%.
        assert first["x"] == pytest.approx(-30.0)
        assert first["width"] == pytest.approx(20.0)

    def test_forward_loss_stops_emission_and_reports_the_frame(self):
        keyframes, lost = mask_bboxes_to_keyframes(
            _boxes(10, lost_from=4),
            fps=FPS,
            clip_start=0.0,
            anchor_index=0,
            frame_size=FRAME,
        )
        assert lost == 4
        assert all(kf["frame"] < 4 for kf in keyframes)

    def test_bidirectional_walks_both_ways_from_the_anchor(self):
        keyframes, lost = mask_bboxes_to_keyframes(
            _boxes(9),
            fps=FPS,
            clip_start=0.0,
            anchor_index=4,
            frame_size=FRAME,
            direction="both",
        )
        assert lost is None
        frames = [kf["frame"] for kf in keyframes]
        assert frames == sorted(frames)
        assert min(frames) == 0 and max(frames) == 8

    def test_backward_only_never_reports_a_forward_loss(self):
        keyframes, lost = mask_bboxes_to_keyframes(
            _boxes(6, lost_from=5),
            fps=FPS,
            clip_start=0.0,
            anchor_index=4,
            frame_size=FRAME,
            direction="backward",
        )
        assert lost is None
        assert all(kf["frame"] <= 4 for kf in keyframes)

    def test_long_clips_stay_inside_the_keyframe_budget(self):
        keyframes, _ = mask_bboxes_to_keyframes(
            _boxes(18000),
            fps=FPS,
            clip_start=0.0,
            anchor_index=0,
            frame_size=FRAME,
        )
        assert len(keyframes) <= 130  # DEFAULT_KEYFRAME_BUDGET plus endpoints


class TestBackendSelection:
    def test_propagation_outranks_mil_and_needs_no_env_flag(self, monkeypatch):
        import app.jobs.mask_track as mask_track_module

        class _NoCSRT:
            @staticmethod
            def TrackerMIL_create():  # pragma: no cover
                return object()

        monkeypatch.setitem(__import__("sys").modules, "cv2", _NoCSRT())
        monkeypatch.setattr(
            "app.services.propagation_tracker.propagation_available", lambda: True
        )
        monkeypatch.delenv("MASK_TRACK_ALLOW_MIL", raising=False)
        backend, reason = mask_track_module.tracker_availability()
        assert backend == "propagate" and reason is None

    def test_without_any_backend_the_reason_names_both_remedies(self, monkeypatch):
        class _Bare:
            pass

        monkeypatch.setitem(__import__("sys").modules, "cv2", _Bare())
        monkeypatch.setattr(
            "app.services.propagation_tracker.propagation_available", lambda: False
        )
        monkeypatch.delenv("MASK_TRACK_ALLOW_MIL", raising=False)
        backend, reason = tracker_availability()
        assert backend is None
        assert "opencv-contrib" in reason and "setup_ml_env" in reason


class TestJobUsesPropagation:
    def test_propagate_backend_lands_the_same_result_shape(
        self, db_session, make_video, monkeypatch
    ):
        video = make_video(duration=10, file_path="https://cdn.example.test/v.mp4")
        row = AiResult(
            video_id=video.id,
            result_type="mask_track",
            status="queued",
            result_data={
                "mask": {"x": 10, "y": 10, "w": 20, "h": 20},
                "direction": "both",
                "anchorTime": 1.0,
                "clipStart": 2.0,
                "clipEnd": 6.0,
            },
        )
        db_session.add(row)
        db_session.commit()

        monkeypatch.setattr(
            "app.jobs.mask_track.tracker_availability", lambda: ("propagate", None)
        )
        seen: dict[str, object] = {}

        def _fake_track(source, **kwargs):
            seen.update(kwargs, source=source)
            return {
                "keyframes": [
                    {"frame": 90, "t": 1.0, "x": 10.0, "y": 10.0, "w": 20.0, "h": 20.0,
                     "rotation": 0.0},
                    {"frame": 96, "t": 1.2, "x": 11.0, "y": 10.0, "w": 20.0, "h": 20.0,
                     "rotation": 0.0},
                ],
                "lostAtFrame": 120,
                "fps": 30.0,
            }

        monkeypatch.setattr(
            "app.services.propagation_tracker.track_by_propagation", _fake_track
        )
        monkeypatch.setattr("app.jobs.mask_track.SessionLocal", lambda: db_session)
        monkeypatch.setattr(db_session, "close", lambda: None)

        mask_track_job(row.id)

        db_session.refresh(row)
        assert row.status == "partial"
        data = row.result_data
        assert data["status"] == "partial"
        assert [kf["frame"] for kf in data["keyframes"]] == [90, 96]
        # lostAt is CLIP-relative seconds: 120/30 - clipStart(2.0) = 2.0.
        assert data["lostAt"] == pytest.approx(2.0)
        assert seen["direction"] == "both" and seen["clip_start"] == 2.0
