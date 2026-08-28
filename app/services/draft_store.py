"""The single write path for the rough-cut draft.

Before this module existed, five writers raced on one JSONB blob with no
coordination: the editor's debounced PUT (whole-document replace), the GET
handler's legacy-merge branch (which committed a write on a read), the
transcription worker's auto-edit seed, the effect worker's `_attach_to_draft`,
and the Director's apply/revert. Writers found the row by `video_id` directly,
bypassing the workspace resolver, so they could write to a different row than
the editor was reading — and the "canonical owner" itself was resolved as the
newest source video by `updated_at`, which moved under live sessions.

This module fixes the causes, not the symptoms:

- The draft is one `RoughCutDraft` row **per project**, revisioned and
  checksummed. Ownership never moves implicitly.
- Every write goes through `save_draft` (foreground, with an
  `expected_revision` compare) or `mutate_draft` (background read-modify-write
  with a bounded retry loop). Both take a row lock for the compare-and-swap.
- Reads never write. The legacy asset-draft merge that used to run inside the
  GET handler happens here, in memory, and is only persisted by the next save.
- Every revision stores a compressed full snapshot in
  `rough_cut_draft_revisions`, which is what makes cross-session revert and
  the harness's inverse manifests trustworthy.
- During the migration window every save is mirrored into the legacy
  `ai_results` row, so readers that have not yet moved (the project pipeline
  widget, older clients) keep seeing a consistent draft.

The `"rangeEditVersion" in payload` heuristic that guarded auto-edit against
clobbering human work survives via `user_edited_at`/`last_writer`, which are
explicit rather than inferred from a sentinel key that only persists because
the API model happens to be `extra="allow"`.
"""

from __future__ import annotations

import hashlib
import json
import logging
import zlib
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

from sqlalchemy.orm import Session

from app.db.models import AiResult, Project, RoughCutDraft, RoughCutDraftRevision, Video
from app.services.rough_cut_workspace import (
    ROUGH_CUT_DRAFT_TYPE,
    WORKSPACE_PERSISTENCE_KEY,
    WORKSPACE_SCHEMA_VERSION,
    _draft_row as _legacy_draft_row,
    _merge_legacy_layers,
    _persistence_metadata,
    is_rough_cut_asset,
    latest_project_source_video,
)

logger = logging.getLogger(__name__)

#: Revisions retained per draft beyond those referenced by harness runs.
SNAPSHOT_RETENTION = 200

#: Writers that count as a human editing session.
HUMAN_WRITERS = {"editor"}


class DraftConflict(Exception):
    """`expected_revision` did not match the stored revision."""

    def __init__(self, current_revision: int, current_checksum: str | None) -> None:
        super().__init__(
            f"Draft has moved to revision {current_revision}; re-read before saving."
        )
        self.current_revision = current_revision
        self.current_checksum = current_checksum


@dataclass
class DraftView:
    project_id: int
    video_id: int | None
    payload: dict[str, Any]
    revision: int
    checksum: str | None
    user_edited_at: datetime | None
    last_writer: str | None
    #: None when the project has never been saved through the store — the
    #: payload then comes from the legacy row(s), read-only.
    row: RoughCutDraft | None


def canonical_json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def checksum_payload(payload: dict[str, Any]) -> str:
    return "sha256:" + hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def resolve_project_for_video(db: Session, video: Video) -> Project:
    project = db.query(Project).filter(Project.id == video.project_id).first()
    if project is None:  # pragma: no cover — FK guarantees this in practice
        raise ValueError(f"Video {video.id} has no project")
    return project


def _legacy_payload(db: Session, project_id: int) -> tuple[dict[str, Any], int | None]:
    """The draft as the legacy storage sees it, merged in memory, never committed.

    Returns `(payload, owner_video_id)`. Mirrors the old
    `get_workspace_draft` merge, minus its write-on-read.
    """
    owner = latest_project_source_video(db, project_id)
    payload: dict[str, Any] = {}
    if owner is not None:
        row = _legacy_draft_row(db, owner.id)
        if row is not None and isinstance(row.result_data, dict):
            payload = deepcopy(row.result_data)

    metadata = _persistence_metadata(payload)
    if int(metadata.get("schemaVersion") or 0) >= WORKSPACE_SCHEMA_VERSION:
        return payload, owner.id if owner else None

    # One-time merge of drafts that were saved below imported-asset video ids.
    legacy_rows = (
        db.query(AiResult)
        .join(Video, Video.id == AiResult.video_id)
        .filter(
            Video.project_id == project_id,
            AiResult.result_type == ROUGH_CUT_DRAFT_TYPE,
        )
        .order_by(AiResult.updated_at.asc(), AiResult.id.asc())
        .all()
    )
    migrated_ids: list[int] = []
    for legacy_row in legacy_rows:
        legacy_video = db.query(Video).filter(Video.id == legacy_row.video_id).first()
        if legacy_video is None or (owner is not None and legacy_video.id == owner.id):
            continue
        if not is_rough_cut_asset(legacy_video):
            continue
        if not isinstance(legacy_row.result_data, dict):
            continue
        _merge_legacy_layers(payload, legacy_row.result_data)
        migrated_ids.append(legacy_row.video_id)

    if payload or migrated_ids:
        metadata.update(
            {
                "schemaVersion": WORKSPACE_SCHEMA_VERSION,
                "legacyDraftVideoIds": sorted(set(migrated_ids)),
            }
        )
        payload[WORKSPACE_PERSISTENCE_KEY] = metadata
    return payload, owner.id if owner else None


def get_draft(db: Session, project_id: int) -> DraftView:
    """Read the draft. Never writes."""
    row = (
        db.query(RoughCutDraft)
        .filter(RoughCutDraft.project_id == project_id)
        .first()
    )
    if row is not None:
        return DraftView(
            project_id=project_id,
            video_id=row.video_id,
            payload=deepcopy(row.payload) if isinstance(row.payload, dict) else {},
            revision=int(row.revision or 0),
            checksum=row.checksum,
            user_edited_at=row.user_edited_at,
            last_writer=row.last_writer,
            row=row,
        )
    payload, owner_video_id = _legacy_payload(db, project_id)
    return DraftView(
        project_id=project_id,
        video_id=owner_video_id,
        payload=payload,
        revision=0,
        checksum=checksum_payload(payload) if payload else None,
        user_edited_at=None,
        last_writer=None,
        row=None,
    )


def get_draft_for_video(db: Session, video: Video) -> DraftView:
    return get_draft(db, video.project_id)


def _locked_row(db: Session, project_id: int) -> RoughCutDraft | None:
    return (
        db.query(RoughCutDraft)
        .filter(RoughCutDraft.project_id == project_id)
        .with_for_update()
        .first()
    )


def _snapshot(db: Session, row: RoughCutDraft, *, parent_revision: int | None,
              writer: str, source_id: str | None, user_id: int | None) -> None:
    db.add(
        RoughCutDraftRevision(
            draft_id=row.id,
            revision=row.revision,
            parent_revision=parent_revision,
            checksum=row.checksum,
            snapshot_zlib=zlib.compress(canonical_json(row.payload or {}).encode("utf-8")),
            writer=writer,
            source_id=source_id,
            created_by=user_id,
        )
    )
    # Retention: keep the recent window. Harness runs pin the revisions they
    # need by copying them into their own manifests, so a plain window is safe.
    floor = int(row.revision or 0) - SNAPSHOT_RETENTION
    if floor > 0:
        db.query(RoughCutDraftRevision).filter(
            RoughCutDraftRevision.draft_id == row.id,
            RoughCutDraftRevision.revision < floor,
        ).delete(synchronize_session=False)


def load_revision(db: Session, draft_id: int, revision: int) -> dict[str, Any] | None:
    row = (
        db.query(RoughCutDraftRevision)
        .filter(
            RoughCutDraftRevision.draft_id == draft_id,
            RoughCutDraftRevision.revision == revision,
        )
        .first()
    )
    if row is None or not row.snapshot_zlib:
        return None
    return json.loads(zlib.decompress(row.snapshot_zlib).decode("utf-8"))


def _mirror_legacy(db: Session, project_id: int, video_id: int | None, payload: dict[str, Any]) -> None:
    """Keep the legacy `ai_results` row consistent during the migration window."""
    owner_id = video_id
    if owner_id is None:
        owner = latest_project_source_video(db, project_id)
        owner_id = owner.id if owner else None
    if owner_id is None:
        return
    legacy = _legacy_draft_row(db, owner_id)
    if legacy is None:
        legacy = AiResult(video_id=owner_id, result_type=ROUGH_CUT_DRAFT_TYPE)
        db.add(legacy)
    legacy.status = "completed"
    legacy.error_message = None
    legacy.result_data = deepcopy(payload)


def save_draft(
    db: Session,
    project_id: int,
    payload: dict[str, Any],
    *,
    writer: str,
    expected_revision: int | None = None,
    video_id: int | None = None,
    user_id: int | None = None,
    source_id: str | None = None,
) -> DraftView:
    """Replace the draft payload, bumping the revision under a row lock.

    `expected_revision` is compared when supplied; a mismatch raises
    `DraftConflict` and changes nothing. Passing None skips the compare — the
    compatibility path for clients that predate revisions, and for background
    writers that already merged through `mutate_draft`.
    """
    if not isinstance(payload, dict):
        raise TypeError("Draft payload must be a dict")

    row = _locked_row(db, project_id)
    created = False
    if row is None:
        # First save through the store adopts whatever the legacy storage held
        # as revision 0's parent, so nothing the user had is lost.
        legacy_payload, owner_video_id = _legacy_payload(db, project_id)
        row = RoughCutDraft(
            project_id=project_id,
            video_id=video_id or owner_video_id,
            revision=0,
            payload=legacy_payload,
            checksum=checksum_payload(legacy_payload) if legacy_payload else None,
        )
        db.add(row)
        db.flush()
        created = True

    current_revision = int(row.revision or 0)
    if expected_revision is not None and not created and expected_revision != current_revision:
        raise DraftConflict(current_revision, row.checksum)
    if expected_revision is not None and created and expected_revision not in (0, current_revision):
        raise DraftConflict(current_revision, row.checksum)

    parent = current_revision
    stored = deepcopy(payload)
    row.payload = stored
    row.revision = parent + 1
    row.checksum = checksum_payload(stored)
    row.last_writer = writer
    if video_id is not None:
        row.video_id = video_id
    if writer in HUMAN_WRITERS:
        row.user_edited_at = datetime.now(timezone.utc)

    _snapshot(db, row, parent_revision=parent, writer=writer, source_id=source_id, user_id=user_id)
    _mirror_legacy(db, project_id, row.video_id, stored)
    db.commit()
    db.refresh(row)
    return DraftView(
        project_id=project_id,
        video_id=row.video_id,
        payload=deepcopy(row.payload),
        revision=int(row.revision),
        checksum=row.checksum,
        user_edited_at=row.user_edited_at,
        last_writer=row.last_writer,
        row=row,
    )


def mutate_draft(
    db: Session,
    project_id: int,
    mutator: Callable[[dict[str, Any]], dict[str, Any] | None],
    *,
    writer: str,
    video_id: int | None = None,
    user_id: int | None = None,
    source_id: str | None = None,
    create: bool = True,
    max_attempts: int = 3,
) -> DraftView | None:
    """Read-modify-write for background writers.

    `mutator` receives a deep copy of the current payload and returns the new
    payload — or None to abort without writing. When `create` is False and no
    draft exists anywhere (store or legacy), nothing happens; that preserves
    `_attach_to_draft`'s "no draft, no-op" contract.
    """
    last_conflict: DraftConflict | None = None
    for _ in range(max(1, max_attempts)):
        view = get_draft(db, project_id)
        if view.row is None and not view.payload and not create:
            return None
        new_payload = mutator(deepcopy(view.payload))
        if new_payload is None:
            return None
        try:
            return save_draft(
                db,
                project_id,
                new_payload,
                writer=writer,
                expected_revision=view.revision,
                video_id=video_id if video_id is not None else view.video_id,
                user_id=user_id,
                source_id=source_id,
            )
        except DraftConflict as conflict:
            last_conflict = conflict
            db.rollback()
            continue
    assert last_conflict is not None
    raise last_conflict
