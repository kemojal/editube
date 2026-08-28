"""The canonical time map between source time and program time.

Three implementations of this mapping already existed and had to agree by
luck: the Director's `CutMap` (app/services/director_context.py), the editor's
`sourceToRenderedTime`/`renderedToSourceTime` (rough-cut-utils.ts), and the
exporter's `_remap_timeline_layers_to_export`. This module is the single spec;
`tests/fixtures/time_map.json` pins its behaviour so the TypeScript port can
be asserted against the same cases (the easing-parity pattern).

Definitions (docs/editing-harness-implementation-plan.md §11.1):

- **source time** — seconds in the original uploaded media.
- **program time** — seconds in the edited output: the keep ranges played in
  their listed order and concatenated. The listed order is authoritative — a
  reordered timeline plays reordered, so ranges are *not* sorted here.
- The **tail** — source time at or past `source_duration` (appended media past
  the A-roll) maps 1:1 after the kept total, mirroring the exporter.

Numeric discipline: all arithmetic happens on integer microseconds so a chain
of conversions cannot accumulate float drift; the public API speaks float
seconds because the draft does.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

US = 1_000_000


def _to_us(seconds: float) -> int:
    return round(float(seconds) * US)


def _to_s(us: int) -> float:
    return us / US


@dataclass(frozen=True)
class _Span:
    source_start_us: int
    source_end_us: int
    program_start_us: int

    @property
    def duration_us(self) -> int:
        return self.source_end_us - self.source_start_us

    @property
    def program_end_us(self) -> int:
        return self.program_start_us + self.duration_us


class TimeMap:
    """Bidirectional source↔program mapping over ordered keep ranges."""

    def __init__(
        self,
        keep_ranges: Iterable[dict[str, Any]],
        *,
        source_duration: float | None = None,
    ) -> None:
        spans: list[_Span] = []
        cursor = 0
        for entry in keep_ranges or []:
            try:
                start_us = _to_us(entry.get("start"))
                end_us = _to_us(entry.get("end"))
            except (TypeError, ValueError):
                continue
            if end_us <= start_us:
                continue
            spans.append(_Span(start_us, end_us, cursor))
            cursor += end_us - start_us
        self._spans = spans
        self._program_total_us = cursor
        self._source_duration_us = (
            _to_us(source_duration) if source_duration is not None else None
        )

    # -- properties -----------------------------------------------------------

    @property
    def program_duration(self) -> float:
        return _to_s(self._program_total_us)

    @property
    def span_count(self) -> int:
        return len(self._spans)

    def is_kept(self, source_time: float) -> bool:
        t = _to_us(source_time)
        return any(s.source_start_us <= t < s.source_end_us for s in self._spans)

    # -- conversions ----------------------------------------------------------

    def to_program(self, source_time: float) -> float | None:
        """Program time for a source time, or None when the moment was cut.

        A source time exactly at a span's end belongs to the next span (the
        half-open convention every existing implementation uses). Times in the
        tail (at/past `source_duration`) map 1:1 after the kept total.
        """
        t = _to_us(source_time)
        for span in self._spans:
            if span.source_start_us <= t < span.source_end_us:
                return _to_s(span.program_start_us + (t - span.source_start_us))
        if self._source_duration_us is not None and t >= self._source_duration_us:
            return _to_s(self._program_total_us + (t - self._source_duration_us))
        # The end of the final span maps to the program end; without this a
        # range's own exclusive endpoint would report as "cut".
        for span in self._spans:
            if t == span.source_end_us:
                return _to_s(span.program_end_us)
        return None

    def to_source(self, program_time: float) -> float | None:
        """Source time for a program time, or None when out of range."""
        t = _to_us(program_time)
        if t < 0:
            return None
        for span in self._spans:
            if span.program_start_us <= t < span.program_end_us:
                return _to_s(span.source_start_us + (t - span.program_start_us))
        if t == self._program_total_us and self._spans:
            return _to_s(self._spans[-1].source_end_us)
        if self._source_duration_us is not None and t >= self._program_total_us:
            return _to_s(self._source_duration_us + (t - self._program_total_us))
        return None

    def clamp_to_program(self, source_start: float, source_end: float) -> tuple[float, float] | None:
        """The program-time span a source interval survives as, or None.

        The interval may cross cut boundaries; the result is the union of its
        surviving pieces expressed as one program interval (pieces are
        contiguous in program time only when the interval spans adjacent
        ranges — callers that need exact pieces should walk spans themselves).
        """
        start_us = _to_us(source_start)
        end_us = _to_us(source_end)
        best_start: int | None = None
        best_end: int | None = None
        for span in self._spans:
            lo = max(start_us, span.source_start_us)
            hi = min(end_us, span.source_end_us)
            if hi <= lo:
                continue
            p_lo = span.program_start_us + (lo - span.source_start_us)
            p_hi = span.program_start_us + (hi - span.source_start_us)
            best_start = p_lo if best_start is None else min(best_start, p_lo)
            best_end = p_hi if best_end is None else max(best_end, p_hi)
        if best_start is None or best_end is None:
            return None
        return _to_s(best_start), _to_s(best_end)
