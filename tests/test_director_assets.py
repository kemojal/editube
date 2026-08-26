"""Turning planned shots into media, and knowing when they have settled.

Two things carry the weight here.

**The house style reaches every prompt.** It is one string, prepended
identically to all of them, and it is the difference between a montage that
reads as one film and twelve stock photos. It is also invisible when missing —
each image still looks fine on its own — so it is asserted per shot rather than
once.

**Failures are skipped, never faked.** An empty or broken clip on the timeline
is worse than an uninterrupted stretch of the speaker's face, because the user
has to find it and take it out.
"""

from __future__ import annotations

import unittest
from unittest import mock

from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker

from app.db.database import Base
from app.db.models import DirectorPlan, GeneratedMedia, Project, User, Video
from app.services import director_assets


@compiles(JSONB, "sqlite")
def _compile_jsonb_for_sqlite(type_, compiler, **kw):
    return "JSON"


HOUSE_STYLE = "35mm, shallow depth of field, warm practical light from frame left"


def _shot(index: int, source="generate-image", prompt=None, **asset_extra):
    asset = {
        "source": source,
        "prompt": prompt if prompt is not None else f"an overhead shot number {index}",
        "aspectRatio": "16:9",
    }
    asset.update(asset_extra)
    return {"id": f"d{index}", "type": "broll", "asset": asset}


def _plan(*shots, house_style=HOUSE_STYLE):
    return {
        "version": 1,
        "brief": {"houseStylePrefix": house_style},
        "beats": [],
        "directives": list(shots),
    }


class PromptCompositionTests(unittest.TestCase):
    def test_the_house_style_leads_and_the_exclusions_trail(self) -> None:
        """Style first because every shot shares it; exclusions last because
        they correct the result rather than describe the subject."""
        prompt = director_assets.compose_prompt(HOUSE_STYLE, "a cluttered desk at dusk")
        self.assertTrue(prompt.startswith(HOUSE_STYLE))
        self.assertTrue(prompt.endswith(director_assets.NEGATIVE_PROMPT))
        self.assertIn("a cluttered desk at dusk", prompt)

    def test_text_and_watermarks_are_excluded(self) -> None:
        """Models add them unprompted, and either makes B-roll unusable."""
        prompt = director_assets.compose_prompt(HOUSE_STYLE, "a desk")
        for banned in ("text", "watermark", "logo", "subtitles"):
            with self.subTest(banned=banned):
                self.assertIn(banned, prompt.lower())

    def test_stray_full_stops_do_not_double_up(self) -> None:
        prompt = director_assets.compose_prompt("A style.", "A shot.")
        self.assertNotIn("..", prompt)


class _DirectorFixture(unittest.TestCase):
    """Shared setup. Not a test case in its own right — subclassed for the
    fan-out and reconcile suites so neither re-runs the other's tests."""

    def setUp(self) -> None:
        engine = create_engine("sqlite://")
        Base.metadata.create_all(
            engine,
            tables=[
                User.__table__, Project.__table__, Video.__table__,
                DirectorPlan.__table__, GeneratedMedia.__table__,
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
            file_path="/m.mp4", uploader_id=self.user.id,
        )
        self.db.add(self.video)
        self.db.flush()
        self.plan_row = DirectorPlan(
            video_id=self.video.id, project_id=self.project.id, user_id=self.user.id,
            status="planning", tier="standard", allow_video=True, cancel_requested=False,
            progress=0,
        )
        self.db.add(self.plan_row)
        self.db.commit()

        self.enqueued: list[int] = []
        patcher = mock.patch(
            "app.jobs.queue.enqueue_generated_media_job",
            side_effect=lambda media_id: self.enqueued.append(media_id) or "job-1",
        )
        self.addCleanup(patcher.stop)
        patcher.start()

    def tearDown(self) -> None:
        self.db.close()

    def _assets(self):
        return (
            self.db.query(GeneratedMedia)
            .filter(GeneratedMedia.director_plan_id == self.plan_row.id)
            .order_by(GeneratedMedia.id)
            .all()
        )


class FanOutTests(_DirectorFixture):
    def test_every_generated_shot_carries_the_same_house_style(self) -> None:
        """The whole point: twelve prompts, one film."""
        director_assets.request_assets(
            self.db, self.plan_row, _plan(_shot(1), _shot(2), _shot(3))
        )
        assets = self._assets()
        self.assertEqual(len(assets), 3)
        for asset in assets:
            with self.subTest(asset=asset.id):
                self.assertTrue(asset.prompt.startswith(HOUSE_STYLE))

    def test_each_shot_keeps_its_own_subject(self) -> None:
        director_assets.request_assets(self.db, self.plan_row, _plan(_shot(1), _shot(2)))
        prompts = [asset.prompt for asset in self._assets()]
        self.assertIn("number 1", prompts[0])
        self.assertIn("number 2", prompts[1])

    def test_a_plan_with_no_house_style_is_refused(self) -> None:
        """Better to generate nothing than twelve unrelated images."""
        with self.assertRaises(ValueError):
            director_assets.request_assets(self.db, self.plan_row, _plan(_shot(1), house_style=" "))

    def test_provenance_links_each_asset_to_its_shot(self) -> None:
        """The compiler joins them back on this; revert filters on it."""
        director_assets.request_assets(self.db, self.plan_row, _plan(_shot(1), _shot(2)))
        pairs = {(a.director_plan_id, a.director_directive_id) for a in self._assets()}
        self.assertEqual(pairs, {(self.plan_row.id, "d1"), (self.plan_row.id, "d2")})

    def test_moving_shots_become_video_generations(self) -> None:
        director_assets.request_assets(
            self.db, self.plan_row,
            _plan(_shot(1), _shot(2, source="generate-video", durationSeconds=4.0)),
        )
        kinds = {a.director_directive_id: a.kind for a in self._assets()}
        self.assertEqual(kinds, {"d1": "image", "d2": "video"})

    def test_reused_project_footage_generates_nothing(self) -> None:
        result = director_assets.request_assets(
            self.db, self.plan_row, _plan(_shot(1, source="project-media"))
        )
        self.assertEqual(result.created, 0)
        self.assertEqual(self._assets(), [])

    def test_a_shot_with_no_description_is_skipped_not_generated_blank(self) -> None:
        result = director_assets.request_assets(
            self.db, self.plan_row, _plan(_shot(1, prompt="  "), _shot(2))
        )
        self.assertEqual(result.created, 1)
        self.assertEqual(result.skipped, ["d1"])

    def test_rows_exist_before_anything_is_enqueued(self) -> None:
        """A pending tile with a prompt under it beats an empty panel."""
        director_assets.request_assets(self.db, self.plan_row, _plan(_shot(1), _shot(2)))
        self.assertEqual(sorted(self.enqueued), [a.id for a in self._assets()])

    def test_generated_shots_arrive_already_accepted(self) -> None:
        """The plan was the review; approving the same shots twice is noise."""
        director_assets.request_assets(self.db, self.plan_row, _plan(_shot(1)))
        self.assertTrue(self._assets()[0].saved)

    def test_the_aspect_ratio_follows_the_shot(self) -> None:
        director_assets.request_assets(
            self.db, self.plan_row, _plan(_shot(1, aspectRatio="9:16"))
        )
        self.assertEqual(self._assets()[0].aspect_ratio, "9:16")


class ReconcileTests(_DirectorFixture):
    """Advancing the run once its assets settle.

    Done on the API's poll rather than in a worker, because a worker holding a
    slot for the minutes a Veo shot takes would block the whole queue.
    """

    def _generating(self, *statuses):
        self.plan_row.status = "generating"
        for index, status in enumerate(statuses, start=1):
            self.db.add(
                GeneratedMedia(
                    project_id=self.project.id, video_id=self.video.id, kind="image",
                    prompt="p", status=status, progress=100 if status == "ready" else 0,
                    director_plan_id=self.plan_row.id, director_directive_id=f"d{index}",
                    cancel_requested=False, saved=True,
                )
            )
        self.db.commit()

    def test_a_run_still_generating_reports_how_far_along_it_is(self) -> None:
        self._generating("ready", "running", "pending")
        director_assets.reconcile(self.db, self.plan_row)
        self.assertEqual(self.plan_row.status, "generating")
        self.assertIn("1 ready", self.plan_row.stage)

    def test_progress_never_restarts_at_zero(self) -> None:
        """Planning already claimed the first quarter; a reset reads as a stall."""
        self._generating("ready", "pending")
        director_assets.reconcile(self.db, self.plan_row)
        self.assertGreaterEqual(self.plan_row.progress, 25)

    def test_a_run_whose_assets_have_all_landed_becomes_ready(self) -> None:
        self._generating("ready", "ready")
        director_assets.reconcile(self.db, self.plan_row)
        self.assertEqual(self.plan_row.status, "ready")
        self.assertEqual(self.plan_row.progress, 100)

    def test_a_few_failures_are_reported_but_do_not_stop_the_run(self) -> None:
        self._generating("ready", "ready", "ready", "failed")
        director_assets.reconcile(self.db, self.plan_row)
        self.assertEqual(self.plan_row.status, "ready")
        self.assertTrue(any("could not be generated" in w for w in self.plan_row.warnings))

    def test_mostly_failed_is_degraded_rather_than_quietly_thinner(self) -> None:
        """A montage missing half its shots is not the edit that was planned."""
        self._generating("ready", "failed", "failed", "failed")
        director_assets.reconcile(self.db, self.plan_row)
        self.assertEqual(self.plan_row.status, "degraded")

    def test_a_run_with_nothing_to_generate_is_already_ready(self) -> None:
        self.plan_row.status = "generating"
        self.db.commit()
        director_assets.reconcile(self.db, self.plan_row)
        self.assertEqual(self.plan_row.status, "ready")

    def test_a_settled_run_is_left_alone(self) -> None:
        """Reconcile runs on every poll; it must be idempotent."""
        self._generating("ready")
        director_assets.reconcile(self.db, self.plan_row)
        self.assertFalse(director_assets.reconcile(self.db, self.plan_row))

    def test_cancelled_assets_count_as_settled(self) -> None:
        """Otherwise a cancelled run polls forever waiting for them."""
        self._generating("ready", "cancelled")
        director_assets.reconcile(self.db, self.plan_row)
        self.assertIn(self.plan_row.status, {"ready", "degraded"})


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
