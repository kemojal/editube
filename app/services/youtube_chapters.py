"""Build YouTube description chapter blocks (timestamps in description)."""

from __future__ import annotations

from typing import Iterable, Sequence


def format_chapter_timestamp(seconds: int) -> str:
    """Format seconds as H:MM:SS or M:SS for YouTube chapter lines."""
    seconds = max(0, int(seconds))
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    if h > 0:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def chapter_lines_from_rows(
    chapters: Sequence[object],
    *,
    start_attr: str = "start_time",
    title_attr: str = "title",
) -> list[str]:
    """Turn ORM VideoChapter-like rows into '0:00 Title' lines."""
    rows = sorted(
        chapters,
        key=lambda c: (getattr(c, start_attr, 0), getattr(c, "order_index", 0) or 0),
    )
    lines: list[str] = []
    for c in rows:
        st = int(getattr(c, start_attr, 0))
        title = (getattr(c, title_attr, "") or "").strip()
        if not title:
            continue
        lines.append(f"{format_chapter_timestamp(st)} {title}")
    return lines


def merge_description_with_chapters(
    base_description: str | None,
    chapter_lines: Iterable[str],
    *,
    header: str = "\n\n--- Chapters ---\n",
) -> str:
    base = (base_description or "").strip()
    lines = list(chapter_lines)
    if not lines:
        return base
    block = header + "\n".join(lines)
    if not base:
        return block.strip()
    return f"{base}{block}"


def youtube_description_block(chapters: Sequence[object]) -> str:
    """Standalone block (no base description) for preview/copy."""
    lines = chapter_lines_from_rows(chapters)
    if not lines:
        return ""
    return "\n".join(lines)
