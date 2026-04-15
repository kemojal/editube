"""Group review comments by video chapters when present, else adaptive time clusters."""

from __future__ import annotations

from typing import List, Sequence

from sqlalchemy.orm import Session

from app.api.models.review_links import ReviewSceneGroup
from app.db.models import Comment, Video, VideoChapter


def _fmt_tc(sec: int) -> str:
    sec = max(0, int(sec))
    m, s = divmod(sec, 60)
    return f"{m:02d}:{s:02d}"


def build_review_scene_groups(
    db: Session,
    video_id: int,
    comments: Sequence[Comment],
) -> List[ReviewSceneGroup]:
    """Prefer chapter buckets; remaining comments cluster by inter-comment gaps."""
    video = db.query(Video).filter(Video.id == video_id).first()
    duration = int(video.duration or 0) if video else 0
    gap_threshold = max(30, min(180, (duration // 40) if duration > 0 else 90))

    chapters: List[VideoChapter] = (
        db.query(VideoChapter)
        .filter(VideoChapter.video_id == video_id)
        .order_by(VideoChapter.start_time.asc(), VideoChapter.order_index.asc())
        .all()
    )

    def chapter_window(ch: VideoChapter) -> tuple[int, int]:
        start = int(ch.start_time or 0)
        end = int(ch.end_time) if ch.end_time is not None else 10**9
        return start, max(start + 1, end)

    placed: dict[int, tuple[str, str, int, int, int]] = {}
    # key: chapter id or synthetic segment id -> (key, label, count, start_tc, end_tc)
    loose: List[tuple[int, Comment]] = []

    for c in comments:
        if c.timecode is None:
            continue
        sec = int(c.timecode)
        hit: VideoChapter | None = None
        for ch in chapters:
            lo, hi = chapter_window(ch)
            if lo <= sec < hi:
                hit = ch
                break
        if hit is not None:
            cid = hit.id
            label = (hit.title or "Chapter").strip() or "Chapter"
            if cid not in placed:
                placed[cid] = (f"chapter-{cid}", label, 0, sec, sec)
            key, lab, cnt, t0, t1 = placed[cid]
            placed[cid] = (key, lab, cnt + 1, min(t0, sec), max(t1, sec))
        else:
            loose.append((sec, c))

    groups: List[ReviewSceneGroup] = []
    for cid, (key, label, cnt, t0, t1) in sorted(
        placed.items(), key=lambda x: x[1][3]
    ):
        groups.append(
            ReviewSceneGroup(
                key=key,
                label=f"{label} ({_fmt_tc(t0)}–{_fmt_tc(t1)})",
                comment_count=cnt,
                start_timecode=t0,
                end_timecode=t1,
            )
        )

    if not loose:
        return groups

    loose.sort(key=lambda x: x[0])
    clusters: List[List[tuple[int, Comment]]] = []
    for sec, c in loose:
        if not clusters:
            clusters.append([(sec, c)])
            continue
        prev_sec = clusters[-1][-1][0]
        if sec - prev_sec > gap_threshold:
            clusters.append([(sec, c)])
        else:
            clusters[-1].append((sec, c))

    base_idx = 0
    for cl in clusters:
        secs = [s for s, _ in cl]
        t0, t1 = min(secs), max(secs)
        key = f"segment-{base_idx}-{t0}"
        base_idx += 1
        groups.append(
            ReviewSceneGroup(
                key=key,
                label=f"Comments {_fmt_tc(t0)}–{_fmt_tc(t1)}",
                comment_count=len(cl),
                start_timecode=t0,
                end_timecode=t1,
            )
        )

    groups.sort(key=lambda g: g.start_timecode)
    return groups
