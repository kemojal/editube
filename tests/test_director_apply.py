"""Landing a plan on the draft, taking it off, and the contract with the editor.

Two suites here.

`ApplyTests` covers the database side: which runs may be applied, what happens
to a draft that has been edited since, and that reverting leaves the user's own
work alone.

`ParityTests` guards the thing that has no second implementation *yet*. The
backend compiles the draft; the editor rewinds and replays the same plan so the
user can watch it happen (§4.2). Both must land on byte-identical state — if
they diverge the user watches one edit and the autosave writes another. The
fixture is written now, before the replay exists, so the contract is fixed
before the second implementation can drift from it.
"""

from __future__ import annotations

import json
import pathlib
import unittest
from types import SimpleNamespace

from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker

from app.db.database import Base
from app.db.models import (
    AiResult,
    DirectorPlan,
    GeneratedMedia,
    Project,
    User,
    Video,
    VideoTranscription,
)
from app.services import director_apply
from app.services.director_compile import compile_plan
from app.services.director_context import build_context

FIXTURE = json.loads(
    (pathlib.Path(__file__).parent / "fixtures" / "director_compile.json").read_text()
)


@compiles(JSONB, "sqlite")
def _compile_jsonb_for_sqlite(type_, compiler, **kw):
    return "JSON"


class ParityTests(unittest.TestCase):
    """The compiler must keep producing exactly what the fixture records."""

    def _compiled(self):
        data = FIXTURE["input"]
        context = build_context(
            segments=data["segments"],
            keep_ranges=data["keepRanges"],
            source_duration=data["sourceDuration"],
            aspect=data["aspect"],
        )
        assets = {
            key: SimpleNamespace(duration_seconds=None, **value)
            for key, value in data["assets"].items()
        }
        return compile_plan(
            data["draft"],
            data["plan"],
            context=context,
            assets_by_directive=assets,
            plan_id=data["planId"],
            applied_at=data["appliedAt"],
        )

    def test_the_compiled_timeline_matches_the_fixture_exactly(self) -> None:
        result = self._compiled()
        expected = FIXTURE["expected"]
        self.assertEqual(result.draft["timelineMediaItems"], expected["timelineMediaItems"])
        self.assertEqual(result.draft["timelineTracks"], expected["timelineTracks"])
        self.assertEqual(result.draft["clipAttributes"], expected["clipAttributes"])
        self.assertEqual(result.manifest, expected["manifest"])

    def test_a_shot_spanning_a_cut_still_plays_for_its_intended_length(self) -> None:
        """The property the whole coordinate design exists to hold.

        `d2` covers ten seconds of source because a six-second gap was removed
        from the middle of it. Intersected with the kept ranges the way the
        export does, it plays for exactly the four seconds it was asked to.
        """
        items = {item["id"]: item for item in self._compiled().draft["timelineMediaItems"]}
        spanning = items["dir7-d2"]
        kept = [(r["start"], r["end"]) for r in FIXTURE["input"]["keepRanges"]]

        on_screen = sum(
            max(0.0, min(spanning["end"], end) - max(spanning["start"], start))
            for start, end in kept
        )
        self.assertAlmostEqual(on_screen, spanning["playDuration"], places=2)
        # And it genuinely does span the gap, or the assertion above is vacuous.
        self.assertGreater(spanning["end"] - spanning["start"], spanning["playDuration"])

    def test_compiling_is_deterministic(self) -> None:
        """The editor replays the same plan; two runs must not differ."""
        self.assertEqual(
            self._compiled().draft["timelineMediaItems"],
            self._compiled().draft["timelineMediaItems"],
        )


class _ApplyFixture(unittest.TestCase):
    """Shared setup only — subclassed so neither suite re-runs the other's."""

    def setUp(self) -> None:
        engine = create_engine("sqlite://")
        Base.metadata.create_all(
            engine,
            tables=[
                User.__table__, Project.__table__, Video.__table__, AiResult.__table__,
                VideoTranscription.__table__, DirectorPlan.__table__, GeneratedMedia.__table__,
            ],
        )
        self.db = sessionmaker(bind=engine)()

        self.user = User(email="e@example.com", name="Edna", role="creator")
        self.db.add(self.user)
        self.db.flush()
        self.project = Project(name="Launch", creator_id=self.user.id, workspace_id=1)
        self.db.add(self.project)
        self.db.flush()
        self.video = Video(
            project_id=self.project.id, name="Interview", version=1,
            file_path="/m.mp4", uploader_id=self.user.id, duration=20,
        )
        self.db.add(self.video)
        self.db.flush()
        self.db.add(
            VideoTranscription(
                video_id=self.video.id, status="completed",
                segments=FIXTURE["input"]["segments"],
            )
        )
        self.db.commit()

    def tearDown(self) -> None:
        self.db.close()

    def _draft(self, data=None):
        row = AiResult(
            video_id=self.video.id, result_type="rough_cut_draft",
            result_data=data if data is not None else dict(FIXTURE["input"]["draft"]),
        )
        self.db.add(row)
        self.db.commit()
        return row

    def _plan_row(self, status="ready"):
        row = DirectorPlan(
            video_id=self.video.id, project_id=self.project.id, user_id=self.user.id,
            status=status, tier="standard", allow_video=True, cancel_requested=False,
            progress=100, plan=FIXTURE["input"]["plan"],
        )
        self.db.add(row)
        self.db.flush()
        for directive_id, asset in FIXTURE["input"]["assets"].items():
            self.db.add(
                GeneratedMedia(
                    project_id=self.project.id, video_id=self.video.id, kind=asset["kind"],
                    prompt="p", status="ready", url=asset["url"], saved=True,
                    cancel_requested=False, director_plan_id=row.id,
                    director_directive_id=directive_id,
                )
            )
        self.db.commit()
        self.db.refresh(row)
        return row


class ApplyTests(_ApplyFixture):
    def test_applying_puts_the_shots_on_the_draft(self) -> None:
        draft_row = self._draft()
        plan_row = self._plan_row()
        result = director_apply.apply_plan(self.db, plan_row)

        self.assertEqual(result.placed, 2)
        self.db.refresh(draft_row)
        ids = [item["id"] for item in draft_row.result_data["timelineMediaItems"]]
        self.assertEqual(ids, ["dir%s-d1" % plan_row.id, "dir%s-d2" % plan_row.id])

    def test_the_run_records_what_it_added(self) -> None:
        self._draft()
        plan_row = self._plan_row()
        director_apply.apply_plan(self.db, plan_row)

        self.assertEqual(plan_row.status, "applied")
        self.assertIsNotNone(plan_row.applied_at)
        self.assertEqual(len(plan_row.applied_manifest["timelineMediaItemIds"]), 2)

    def test_a_run_that_is_still_working_cannot_be_applied(self) -> None:
        self._draft()
        plan_row = self._plan_row(status="generating")
        with self.assertRaises(director_apply.NotApplicable):
            director_apply.apply_plan(self.db, plan_row)

    def test_a_partly_failed_run_can_still_be_applied(self) -> None:
        """It produced real shots; the user may well want the ones that worked."""
        self._draft()
        plan_row = self._plan_row(status="degraded")
        self.assertEqual(director_apply.apply_plan(self.db, plan_row).placed, 2)

    def test_applying_preserves_editing_done_since_the_plan_was_written(self) -> None:
        """Compiling only ever adds, so this cannot destroy someone's work."""
        draft = dict(FIXTURE["input"]["draft"])
        draft["timelineMediaItems"] = [
            {"id": "mine", "track": "video", "trackId": "track-video-1"}
        ]
        draft["rangeEditVersion"] = 3
        draft_row = self._draft(draft)
        plan_row = self._plan_row()
        director_apply.apply_plan(self.db, plan_row)

        self.db.refresh(draft_row)
        ids = [item["id"] for item in draft_row.result_data["timelineMediaItems"]]
        self.assertIn("mine", ids)
        self.assertEqual(draft_row.result_data["rangeEditVersion"], 3)

    def test_anchors_resolve_against_the_cut_as_it_is_now(self) -> None:
        """A re-cut since planning moves the shots with it.

        This is what anchoring to words rather than timestamps buys: the plan
        does not go stale when the user keeps editing.
        """
        draft = dict(FIXTURE["input"]["draft"])
        draft["keepRanges"] = [{"start": 0, "end": 20}]
        draft_row = self._draft(draft)
        plan_row = self._plan_row()
        director_apply.apply_plan(self.db, plan_row)

        self.db.refresh(draft_row)
        spanning = draft_row.result_data["timelineMediaItems"][1]
        # With nothing cut, the shot no longer has to span a gap.
        self.assertAlmostEqual(
            spanning["end"] - spanning["start"], spanning["playDuration"], places=2
        )

    def test_a_draft_that_does_not_exist_yet_is_created(self) -> None:
        plan_row = self._plan_row()
        director_apply.apply_plan(self.db, plan_row)
        row = (
            self.db.query(AiResult)
            .filter(AiResult.video_id == self.video.id)
            .first()
        )
        self.assertTrue(row.result_data["timelineMediaItems"])


class RevertTests(_ApplyFixture):
    def test_reverting_removes_only_what_the_run_added(self) -> None:
        draft = dict(FIXTURE["input"]["draft"])
        draft["timelineMediaItems"] = [
            {"id": "mine", "track": "video", "trackId": "track-video-1"}
        ]
        draft_row = self._draft(draft)
        plan_row = self._plan_row()
        director_apply.apply_plan(self.db, plan_row)

        self.assertTrue(director_apply.revert(self.db, plan_row))
        self.db.refresh(draft_row)
        self.assertEqual(
            [i["id"] for i in draft_row.result_data["timelineMediaItems"]], ["mine"]
        )

    def test_a_reverted_run_can_be_applied_again_without_re_planning(self) -> None:
        """Nothing is regenerated; the assets are already paid for."""
        self._draft()
        plan_row = self._plan_row()
        director_apply.apply_plan(self.db, plan_row)
        director_apply.revert(self.db, plan_row)

        self.assertEqual(plan_row.status, "ready")
        self.assertEqual(director_apply.apply_plan(self.db, plan_row).placed, 2)

    def test_reverting_a_run_that_was_never_applied_does_nothing(self) -> None:
        self._draft()
        plan_row = self._plan_row()
        self.assertFalse(director_apply.revert(self.db, plan_row))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
