"""Learned preferences for the harness (plan Phase 5) — deterministic and inspectable.

Nothing here is trained. Every "learned" value is a plain, reproducible query
over the user's own harness run history — what they accepted and kept, what
they took back off — so the inspect endpoint can show exactly why each
default is what it is, disabling is one flag, and reset is a timestamp
rather than a deletion (the runs stay; the learner just stops looking
before the cutoff). Governance, stated plainly: only run OUTCOMES and the
parameter values the user themselves chose are read. No media, no
transcripts, no prompt content, and nothing crosses user boundaries — one
user's history never shapes another user's defaults.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from app.db.models import HarnessRun, UserSettings

#: Parameter keys a recipe may learn defaults for. Deliberately a whitelist
#: of STYLE choices — never content (text, labels, ranges, boxes are about
#: this video, not about how the user likes things done).
LEARNABLE_PARAMS: dict[str, tuple[str, ...]] = {
    "subject_behind_text": ("dimBackground", "templateId", "maskQuality", "animationIn"),
    "tracked_callout": ("accent", "quality", "widthPct", "side"),
    "review_fix": (),
}

#: How many recent accepted runs feed the learned defaults.
ACCEPT_SAMPLE = 10
#: How many recent settled runs feed the revert-rate gate.
REVERT_WINDOW = 20
#: Below this many settled runs the gate stays open — no rate is measurable.
MIN_SAMPLE_FOR_GATE = 3
#: Above this revert rate, auto-apply declines and asks for a review.
MAX_AUTO_APPLY_REVERT_RATE = 1 / 3


def _settings_row(db: Session, user_id: int) -> UserSettings | None:
    return db.query(UserSettings).filter(UserSettings.user_id == user_id).first()


def _prefs(db: Session, user_id: int) -> dict[str, Any]:
    row = _settings_row(db, user_id)
    value = getattr(row, "harness_preferences", None) if row else None
    return dict(value) if isinstance(value, dict) else {}


def learning_enabled(db: Session, user_id: int) -> bool:
    return bool(_prefs(db, user_id).get("learningEnabled", True))


def reset_cutoff(db: Session, user_id: int) -> datetime | None:
    raw = _prefs(db, user_id).get("resetAt")
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(str(raw))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _write_prefs(db: Session, user_id: int, patch: dict[str, Any]) -> dict[str, Any]:
    row = _settings_row(db, user_id)
    if row is None:
        row = UserSettings(user_id=user_id)
        db.add(row)
        db.flush()
    merged = {**_prefs(db, user_id), **patch}
    row.harness_preferences = merged
    db.commit()
    return merged


def set_learning_enabled(db: Session, user_id: int, enabled: bool) -> dict[str, Any]:
    return _write_prefs(db, user_id, {"learningEnabled": bool(enabled)})


def reset(db: Session, user_id: int) -> dict[str, Any]:
    """Forget everything learned so far — by cutoff, not deletion."""
    return _write_prefs(
        db, user_id, {"resetAt": datetime.now(timezone.utc).isoformat()}
    )


def _settled_runs(db: Session, user_id: int, recipe_id: str, limit: int) -> list[HarnessRun]:
    cutoff = reset_cutoff(db, user_id)
    query = (
        db.query(HarnessRun)
        .filter(
            HarnessRun.created_by == user_id,
            HarnessRun.recipe_id == recipe_id,
            HarnessRun.state.in_(["ready", "reverted"]),
        )
        .order_by(HarnessRun.created_at.desc(), HarnessRun.id.desc())
    )
    if cutoff is not None:
        query = query.filter(HarnessRun.created_at > cutoff.replace(tzinfo=None))
    return query.limit(limit).all()


def recipe_stats(db: Session, user_id: int, recipe_id: str) -> dict[str, Any]:
    """Accept/revert counts over the recent window — the gate's evidence."""
    runs = _settled_runs(db, user_id, recipe_id, REVERT_WINDOW)
    reverted = sum(1 for run in runs if run.state == "reverted")
    sample = len(runs)
    return {
        "sample": sample,
        "kept": sample - reverted,
        "reverted": reverted,
        "revertRate": round(reverted / sample, 3) if sample else 0.0,
    }


def learned_defaults(db: Session, user_id: int, recipe_id: str) -> dict[str, Any]:
    """Modal style-parameter values from the user's recent kept runs.

    Majority wins; ties go to the most recent choice (runs are scanned
    newest-first and Counter.most_common is insertion-stable on ties).
    Only scalars are learnable, and only whitelisted keys.
    """
    keys = LEARNABLE_PARAMS.get(recipe_id, ())
    if not keys:
        return {}
    kept = [
        run
        for run in _settled_runs(db, user_id, recipe_id, REVERT_WINDOW)
        if run.state == "ready"
    ][:ACCEPT_SAMPLE]
    defaults: dict[str, Any] = {}
    for key in keys:
        values = [
            run.params.get(key)
            for run in kept
            if isinstance(run.params, dict)
            and key in run.params
            and isinstance(run.params.get(key), (str, int, float, bool))
        ]
        if values:
            defaults[key] = Counter(values).most_common(1)[0][0]
    return defaults


def auto_apply_gate(db: Session, user_id: int | None, recipe_id: str | None) -> str | None:
    """Why auto-apply should NOT run for this user+recipe, or None.

    The Phase 5 exit criterion made measurable: auto-apply is limited to
    operations with a low observed revert rate. A user who keeps taking
    this recipe's results back off gets the review step back.
    """
    if user_id is None or not recipe_id:
        return None
    stats = recipe_stats(db, user_id, recipe_id)
    if (
        stats["sample"] >= MIN_SAMPLE_FOR_GATE
        and stats["revertRate"] > MAX_AUTO_APPLY_REVERT_RATE
    ):
        return (
            f"you took {stats['reverted']} of your last {stats['sample']} "
            f"{recipe_id} runs back off, so this one waits for your review"
        )
    return None


def snapshot(db: Session, user_id: int) -> dict[str, Any]:
    """Everything the learner knows, and why — the inspect endpoint's body."""
    prefs = _prefs(db, user_id)
    return {
        "learningEnabled": bool(prefs.get("learningEnabled", True)),
        "resetAt": prefs.get("resetAt"),
        "recipes": {
            recipe_id: {
                "stats": recipe_stats(db, user_id, recipe_id),
                "learnedDefaults": learned_defaults(db, user_id, recipe_id),
                "learnableKeys": list(keys),
            }
            for recipe_id, keys in LEARNABLE_PARAMS.items()
        },
        "governance": (
            "Computed from your own run outcomes and the parameter values you "
            "chose — never from media, transcripts, or other users' history. "
            "Reset forgets by cutoff; your runs are untouched."
        ),
    }
