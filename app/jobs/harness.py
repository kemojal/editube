"""RQ entry point for editing-harness apply runs.

Thin by design: everything interesting lives in
`app.services.harness.executor.execute_apply`, which also runs inline when no
queue is configured (the AI-review pattern), so this wrapper only owns the
session and the last-resort failure write.
"""

from __future__ import annotations

import logging

from app.db.database import SessionLocal

logger = logging.getLogger(__name__)


def harness_apply_job(run_id: int) -> None:
    from app.services.harness.executor import execute_apply

    db = SessionLocal()
    try:
        execute_apply(db, run_id)
    except Exception as exc:  # noqa: BLE001
        try:
            db.rollback()
            from app.db.models import HarnessRun

            run = db.query(HarnessRun).filter(HarnessRun.id == run_id).first()
            if run is not None and run.state not in {"ready", "failed", "cancelled", "conflicted"}:
                run.state = "failed"
                run.error_code = "apply_crashed"
                run.error_detail = str(exc)[:2000]
                run.stage = None
                db.commit()
        except Exception:  # noqa: BLE001
            logger.exception("Harness run %s: failed to record crash", run_id)
        raise
    finally:
        db.close()
