"""The Director applying THROUGH the harness engine (plan Phase 4).

`test_director_apply.py` proves the outcome is the same for the user: shots
land, their edits survive, revert removes only what the run added. This file
proves the *machinery* changed hands — every apply mints a `HarnessRun` that
owns the plan, the inverse manifest and the operation rows, and revert routes
through the harness while keeping the inverse as an audit trail. It also pins
the two edges the migration must not break: pre-migration manifests (no
`harnessRunId`) still revert via the legacy id filter, and a run whose assets
all failed still stamps and reports rather than crashing.
"""

from __future__ import annotations

import json
import pathlib
import unittest

from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker

from app.db.database import Base
from app.db.models import (
    AiResult,
    DirectorPlan,
    GeneratedMedia,
    HarnessOperation,
    HarnessRun,
    Project,
    User,
    Video,
    VideoTranscription,
)
from app.services import director_apply

FIXTURE = json.loads(
    (pathlib.Path(__file__).parent / "fixtures" / "director_compile.json").read_text()
)


@compiles(JSONB, "sqlite")
def _compile_jsonb_for_sqlite(type_, compiler, **kw):
    return "JSON"


class _Fixture(unittest.TestCase):
    def setUp(self) -> None:
        from tests.conftest import all_public_tables

        engine = create_engine("sqlite://")
        Base.metadata.create_all(engine, tables=all_public_tables())
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

    def _plan_row(self, *, with_assets=True):
        row = DirectorPlan(
            video_id=self.video.id, project_id=self.project.id, user_id=self.user.id,
            status="ready", tier="standard", allow_video=True, cancel_requested=False,
            progress=100, plan=FIXTURE["input"]["plan"],
        )
        self.db.add(row)
        self.db.flush()
        if with_assets:
            for directive_id, asset in FIXTURE["input"]["assets"].items():
                self.db.add(
                    GeneratedMedia(
                        project_id=self.project.id, video_id=self.video.id,
                        kind=asset["kind"], prompt="p", status="ready",
                        url=asset["url"], saved=True, cancel_requested=False,
                        director_plan_id=row.id, director_directive_id=directive_id,
                    )
                )
        self.db.commit()
        self.db.refresh(row)
        return row

    def _run(self, plan_row) -> HarnessRun:
        run_id = plan_row.applied_manifest["harnessRunId"]
        return self.db.query(HarnessRun).filter(HarnessRun.id == run_id).one()


class ApplyMintsARun(_Fixture):
    def test_the_run_owns_the_plan_and_both_manifests(self) -> None:
        self._draft()
        plan_row = self._plan_row()
        director_apply.apply_plan(self.db, plan_row)

        run = self._run(plan_row)
        self.assertEqual(run.recipe_id, "director_broll")
        self.assertEqual(run.state, "ready")
        self.assertEqual(run.params["directorPlanId"], plan_row.id)
        ops = run.plan["operations"]
        self.assertEqual([op["type"] for op in ops],
                         ["timeline.place_media"] * 2)
        self.assertEqual([op["id"] for op in ops], ["shot_d1", "shot_d2"])
        # Everything the run added is restorable, and the run knows its draft.
        self.assertTrue(run.inverse_manifest)
        self.assertEqual(len(run.applied_manifest["timelineMediaItemIds"]), 2)
        self.assertIsNotNone(run.applied_draft_revision)
        self.assertIsNotNone(run.result_checksum)
        self.assertIn(run.verification_report["status"], {"pass", "warnings"})

    def test_operation_rows_are_created_and_marked_applied(self) -> None:
        self._draft()
        plan_row = self._plan_row()
        director_apply.apply_plan(self.db, plan_row)

        run = self._run(plan_row)
        rows = (
            self.db.query(HarnessOperation)
            .filter(HarnessOperation.run_id == run.id)
            .all()
        )
        self.assertEqual({r.operation_key for r in rows}, {"shot_d1", "shot_d2"})
        self.assertEqual({r.state for r in rows}, {"applied"})

    def test_the_manifest_on_the_plan_row_names_the_run(self) -> None:
        self._draft()
        plan_row = self._plan_row()
        director_apply.apply_plan(self.db, plan_row)
        self.assertEqual(
            plan_row.applied_manifest["harnessRunId"], self._run(plan_row).id
        )

    def test_a_run_with_no_usable_assets_still_stamps_and_reports(self) -> None:
        """Every directive failed to generate; the apply must not crash."""
        self._draft()
        plan_row = self._plan_row(with_assets=False)
        result = director_apply.apply_plan(self.db, plan_row)

        self.assertEqual(result.placed, 0)
        self.assertTrue(result.warnings)
        self.assertEqual(plan_row.status, "applied")
        run = self._run(plan_row)
        self.assertEqual(run.state, "ready")
        self.assertEqual(run.applied_manifest["timelineMediaItemIds"], [])


class RevertRoutesThroughTheHarness(_Fixture):
    def test_revert_keeps_the_inverse_as_an_audit_trail(self) -> None:
        self._draft()
        plan_row = self._plan_row()
        director_apply.apply_plan(self.db, plan_row)
        run = self._run(plan_row)
        inverse_before = list(run.inverse_manifest)

        self.assertTrue(director_apply.revert(self.db, plan_row))
        self.db.refresh(run)
        self.assertEqual(run.state, "reverted")
        # The whole point of the migration: the old path nulled this.
        self.assertEqual(run.inverse_manifest, inverse_before)
        self.assertEqual(plan_row.status, "ready")
        self.assertIsNone(plan_row.applied_manifest)
        self.assertIsNone(plan_row.applied_at)

    def test_reapply_after_revert_mints_a_fresh_run(self) -> None:
        self._draft()
        plan_row = self._plan_row()
        director_apply.apply_plan(self.db, plan_row)
        first = self._run(plan_row).id
        director_apply.revert(self.db, plan_row)
        director_apply.apply_plan(self.db, plan_row)
        self.assertNotEqual(self._run(plan_row).id, first)

    def test_a_missing_run_still_unsticks_the_director_row(self) -> None:
        """The run row was deleted out from under us; the UI must not wedge."""
        self._draft()
        plan_row = self._plan_row()
        director_apply.apply_plan(self.db, plan_row)
        run = self._run(plan_row)
        self.db.query(HarnessOperation).filter(
            HarnessOperation.run_id == run.id
        ).delete()
        self.db.delete(run)
        self.db.commit()

        self.assertTrue(director_apply.revert(self.db, plan_row))
        self.assertEqual(plan_row.status, "ready")
        self.assertIsNone(plan_row.applied_manifest)


class LegacyManifestsStillRevert(_Fixture):
    def test_a_pre_migration_manifest_uses_the_id_filter(self) -> None:
        draft = dict(FIXTURE["input"]["draft"])
        draft["timelineMediaItems"] = [
            {"id": "mine", "track": "video", "trackId": "track-video-1"},
            {"id": "dir9-d1", "track": "video", "trackId": "track-broll-1"},
        ]
        draft["directorPlanId"] = 9
        draft_row = self._draft(draft)
        plan_row = self._plan_row()
        # An applied row written by the code before this migration.
        plan_row.status = "applied"
        plan_row.applied_manifest = {
            "timelineMediaItemIds": ["dir9-d1"],
            "clipAttributeKeys": [],
            "trackIds": ["track-broll-1"],
        }
        self.db.commit()

        self.assertTrue(director_apply.revert(self.db, plan_row))
        self.db.refresh(draft_row)
        ids = [i["id"] for i in draft_row.result_data["timelineMediaItems"]]
        self.assertEqual(ids, ["mine"])
        self.assertNotIn("directorPlanId", draft_row.result_data)
        self.assertEqual(plan_row.status, "ready")
        # No harness run was ever involved.
        self.assertEqual(self.db.query(HarnessRun).count(), 0)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
