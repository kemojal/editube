"""Export video comments to interchange formats (CSV, EDL, FCPXML, Premiere-style XML)."""

from __future__ import annotations

import csv
import io
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from typing import List, Sequence

from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
from reportlab.lib import colors

from app.db.models import Comment


def _xml_escape(text: str) -> str:
    return (
        (text or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def sec_to_timecode(seconds: int | None, fps: int = 30) -> str:
    s = max(0, int(seconds or 0))
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    fr = 0
    return f"{h:02d}:{m:02d}:{sec:02d}:{fr:02d}"


def _author(c: Comment) -> str:
    if c.user_id and c.user:
        return c.user.name or c.user.email or "User"
    return (c.guest_name or "Guest").strip()


def _flat_comments_in_order(comments: Sequence[Comment]) -> List[Comment]:
    """Depth-first: parent then nested replies (by created_at)."""
    by_parent: dict[int | None, List[Comment]] = {}
    for c in comments:
        by_parent.setdefault(c.parent_id, []).append(c)
    for lst in by_parent.values():
        lst.sort(key=lambda x: (x.timecode or 0, x.created_at))

    out: List[Comment] = []

    def walk(pid: int | None):
        for c in by_parent.get(pid, []):
            out.append(c)
            walk(c.id)

    walk(None)
    return out


def export_comments_csv(comments: Sequence[Comment]) -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["id", "parent_id", "timecode_sec", "timecode", "kind", "status", "author", "text"])
    for c in _flat_comments_in_order(comments):
        w.writerow(
            [
                c.id,
                c.parent_id or "",
                c.timecode or 0,
                sec_to_timecode(c.timecode),
                getattr(c, "kind", "comment") or "comment",
                getattr(c, "status", "open") or "open",
                _author(c),
                (c.text or "").replace("\n", " ").replace("\r", ""),
            ]
        )
    return buf.getvalue().encode("utf-8")


def export_comments_edl(comments: Sequence[Comment]) -> bytes:
    lines: List[str] = ["TITLE: Editube comments", "FCM: NON-DROP FRAME"]
    idx = 1
    for c in _flat_comments_in_order(comments):
        if c.parent_id is not None:
            continue
        tc = sec_to_timecode(c.timecode)
        tc_end = sec_to_timecode((c.timecode or 0) + 1)
        safe = re.sub(r"[^\w\s\-.,]", "", (c.text or "")[:60]).strip() or "comment"
        lines.append(
            f"{idx:03d}  AX       V     C        "
            f"{tc} {tc_end} {tc} {tc_end}\n* FROM CLIP NAME: {_xml_escape(safe)[:80]}"
        )
        idx += 1
    return ("\n".join(lines) + "\n").encode("utf-8")


def export_comments_fcpxml(comments: Sequence[Comment], duration_sec: int = 3600) -> bytes:
    root = ET.Element("fcpxml", {"version": "1.9"})
    lib = ET.SubElement(root, "library")
    ev = ET.SubElement(lib, "event", {"name": "Editube"})
    proj = ET.SubElement(ev, "project", {"name": "Comment markers"})
    seq = ET.SubElement(proj, "sequence", {"duration": f"{duration_sec}s", "tcStart": "0s"})
    spine = ET.SubElement(seq, "spine")
    gap = ET.SubElement(
        spine,
        "gap",
        {"name": "Comments", "offset": "0s", "duration": f"{duration_sec}s"},
    )
    for c in _flat_comments_in_order(comments):
        if c.parent_id is not None:
            continue
        start = max(0, int(c.timecode or 0))
        label = _xml_escape(((c.text or "")[:200]).replace("\n", " "))
        ET.SubElement(
            gap,
            "marker",
            {
                "start": f"{start}s",
                "duration": "1s",
                "value": label or "comment",
            },
        )
    raw = ET.tostring(root, encoding="utf-8", xml_declaration=True)
    return raw


def export_comments_premiere_xml(comments: Sequence[Comment], duration_sec: int = 3600) -> bytes:
    """Minimal XMEML-style marker list for Premiere / interchange testing."""
    root = ET.Element("xmeml", {"version": "4"})
    seq = ET.SubElement(root, "sequence")
    ET.SubElement(seq, "name").text = "Editube comments"
    ET.SubElement(seq, "duration").text = str(duration_sec * 30)
    media = ET.SubElement(seq, "media")
    video = ET.SubElement(media, "video")
    track = ET.SubElement(video, "track")
    for c in _flat_comments_in_order(comments):
        if c.parent_id is not None:
            continue
        start = max(0, int(c.timecode or 0))
        clip = ET.SubElement(track, "clipitem", {"id": f"c{c.id}"})
        ET.SubElement(clip, "name").text = ((c.text or "")[:120] or "comment").replace("\n", " ")
        ET.SubElement(clip, "start").text = str(start * 30)
        ET.SubElement(clip, "end").text = str((start + 1) * 30)
        ET.SubElement(clip, "in").text = "0"
        ET.SubElement(clip, "out").text = "30"
    raw = ET.tostring(root, encoding="utf-8", xml_declaration=True)
    return raw


def export_comments_pdf(comments: Sequence[Comment], video_name: str) -> bytes:
    rows = [["TC", "Kind", "Status", "Author", "Text"]]
    for c in _flat_comments_in_order(comments):
        rows.append(
            [
                sec_to_timecode(c.timecode),
                getattr(c, "kind", "comment") or "comment",
                getattr(c, "status", "open") or "open",
                _author(c),
                ((c.text or "")[:500]).replace("\n", " "),
            ]
        )
    bio = io.BytesIO()
    doc = SimpleDocTemplate(bio, pagesize=letter)
    styles = getSampleStyleSheet()
    story = [
        Paragraph(_xml_escape(f"Comments — {video_name}"), styles["Title"]),
        Spacer(1, 12),
        Paragraph(
            f"Generated (UTC): {datetime.now(timezone.utc).isoformat()}",
            styles["Normal"],
        ),
        Spacer(1, 18),
    ]
    t = Table(rows, repeatRows=1, colWidths=[72, 60, 60, 90, 240])
    t.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.grey),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.whitesmoke),
                ("GRID", (0, 0), (-1, -1), 0.25, colors.grey),
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("FONTSIZE", (0, 0), (-1, -1), 8),
            ]
        )
    )
    story.append(t)
    doc.build(story)
    out = bio.getvalue()
    bio.close()
    return out


def export_comments(comments: Sequence[Comment], fmt: str, video_name: str = "Video") -> tuple[bytes, str, str]:
    f = fmt.lower().strip()
    if f == "csv":
        return export_comments_csv(comments), "text/csv", "comments.csv"
    if f == "edl":
        return export_comments_edl(comments), "text/plain", "comments.edl"
    if f in ("fcpxml", "fcp"):
        return export_comments_fcpxml(comments), "application/xml", "comments.fcpxml"
    if f in ("premiere", "xml", "xmeml"):
        return export_comments_premiere_xml(comments), "application/xml", "comments_premiere.xml"
    if f == "pdf":
        return export_comments_pdf(comments, video_name), "application/pdf", "comments.pdf"
    raise ValueError(f"Unknown export format: {fmt}")
