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


def export_comments_ae_jsx(comments: Sequence[Comment]) -> bytes:
    """Generate After Effects ExtendScript (.jsx) that adds markers to the active comp."""
    lines: List[str] = [
        "// Editube comment markers — run in After Effects ExtendScript",
        "var comp = app.project.activeItem;",
        "if (comp && comp instanceof CompItem) {",
    ]
    for c in _flat_comments_in_order(comments):
        if c.parent_id is not None:
            continue
        tc = max(0, int(c.timecode or 0))
        safe = ((c.text or "")[:200]).replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")
        author = _author(c).replace('"', '\\"')
        lines.append(f'  var m = new MarkerValue("{safe}");')
        lines.append(f'  m.comment = "{author}";')
        lines.append(f"  comp.markerProperty.setValueAtTime({tc}, m);")
    lines.append("} else {")
    lines.append('  alert("No active composition — open a comp first.");')
    lines.append("}")
    return ("\n".join(lines) + "\n").encode("utf-8")


def export_comments_resolve_otio(comments: Sequence[Comment], duration_sec: int = 3600) -> bytes:
    """Generate OpenTimelineIO JSON with markers for DaVinci Resolve.

    Resolve can import OTIO files via Workflow Integration API or File > Import.
    This creates a minimal timeline with marker annotations.
    """
    markers = []
    for c in _flat_comments_in_order(comments):
        if c.parent_id is not None:
            continue
        tc = max(0, int(c.timecode or 0))
        text = ((c.text or "")[:200]).replace("\n", " ")
        author = _author(c)
        markers.append({
            "OTIO_SCHEMA": "Marker.2",
            "metadata": {"editube_id": c.id, "author": author},
            "name": text[:80] or "comment",
            "color": "RED",
            "marked_range": {
                "OTIO_SCHEMA": "TimeRange.1",
                "start_time": {
                    "OTIO_SCHEMA": "RationalTime.1",
                    "value": tc * 24,
                    "rate": 24.0,
                },
                "duration": {
                    "OTIO_SCHEMA": "RationalTime.1",
                    "value": 24,
                    "rate": 24.0,
                },
            },
            "comment": text,
        })

    import json
    otio = {
        "OTIO_SCHEMA": "Timeline.1",
        "metadata": {"source": "editube"},
        "name": "Editube Comments",
        "tracks": {
            "OTIO_SCHEMA": "Stack.1",
            "children": [
                {
                    "OTIO_SCHEMA": "Track.1",
                    "name": "Comments",
                    "kind": "Video",
                    "children": [
                        {
                            "OTIO_SCHEMA": "Gap.1",
                            "source_range": {
                                "OTIO_SCHEMA": "TimeRange.1",
                                "start_time": {
                                    "OTIO_SCHEMA": "RationalTime.1",
                                    "value": 0,
                                    "rate": 24.0,
                                },
                                "duration": {
                                    "OTIO_SCHEMA": "RationalTime.1",
                                    "value": duration_sec * 24,
                                    "rate": 24.0,
                                },
                            },
                            "markers": markers,
                        }
                    ],
                }
            ],
        },
    }
    return json.dumps(otio, indent=2).encode("utf-8")


def import_comments_from_fcpxml(xml_bytes: bytes) -> List[dict]:
    """Parse FCPXML markers and return a list of marker dicts.

    Each dict has shape: {"timecode_sec": int, "text": str, "end_timecode_sec": int|None}.
    The caller is responsible for creating Comment rows from these.
    """
    root = ET.fromstring(xml_bytes)
    markers: List[dict] = []

    def _parse_offset(val: str) -> int:
        """Parse FCPXML time values like '10s', '100/30s', '10.5s'."""
        if not val:
            return 0
        val = val.strip().rstrip("s")
        if "/" in val:
            parts = val.split("/")
            return int(float(parts[0]) / float(parts[1]))
        return int(float(val))

    for marker in root.iter("marker"):
        start = _parse_offset(marker.get("start", "0s"))
        dur = _parse_offset(marker.get("duration", "1s"))
        text = marker.get("value", "").strip() or marker.get("name", "comment")
        markers.append({
            "timecode_sec": start,
            "end_timecode_sec": start + dur if dur > 1 else None,
            "text": text,
        })

    return markers


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
    if f in ("ae", "jsx", "after_effects"):
        return export_comments_ae_jsx(comments), "text/plain", "comments_ae.jsx"
    if f in ("otio", "resolve"):
        return export_comments_resolve_otio(comments), "application/json", "comments.otio"
    raise ValueError(f"Unknown export format: {fmt}")

