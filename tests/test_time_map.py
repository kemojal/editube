"""The canonical time map, pinned by the shared fixture.

`tests/fixtures/time_map.json` is the contract file the TypeScript port will be
asserted against (the easing-parity pattern), so this suite deliberately reads
its cases from the fixture rather than restating them.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.services.time_map import TimeMap

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "time_map.json").read_text())


def _cases():
    for case in FIXTURE["cases"]:
        yield pytest.param(case, id=case["name"])


@pytest.mark.parametrize("case", _cases())
def test_program_duration(case):
    tm = TimeMap(case["keepRanges"], source_duration=case.get("sourceDuration"))
    assert tm.program_duration == pytest.approx(case["programDuration"], abs=1e-6)


@pytest.mark.parametrize("case", _cases())
def test_to_program(case):
    tm = TimeMap(case["keepRanges"], source_duration=case.get("sourceDuration"))
    for row in case["toProgram"]:
        got = tm.to_program(row["source"])
        if row["program"] is None:
            assert got is None, f"source {row['source']} should be cut"
        else:
            assert got == pytest.approx(row["program"], abs=1e-6)


@pytest.mark.parametrize("case", _cases())
def test_to_source(case):
    tm = TimeMap(case["keepRanges"], source_duration=case.get("sourceDuration"))
    for row in case["toSource"]:
        got = tm.to_source(row["program"])
        assert got == pytest.approx(row["source"], abs=1e-6)


@pytest.mark.parametrize("case", _cases())
def test_round_trip_survives_the_map(case):
    """Every kept source instant must round-trip source→program→source exactly."""
    tm = TimeMap(case["keepRanges"], source_duration=case.get("sourceDuration"))
    for rng in case["keepRanges"]:
        if rng["end"] <= rng["start"]:
            continue
        step = (rng["end"] - rng["start"]) / 7
        t = rng["start"]
        while t < rng["end"] - 1e-9:
            program = tm.to_program(t)
            assert program is not None
            back = tm.to_source(program)
            assert back == pytest.approx(t, abs=1e-6)
            t += step


def test_no_float_drift_across_many_conversions():
    """A thousand chained conversions must not accumulate error (integer µs)."""
    tm = TimeMap([{"start": 0.1, "end": 0.3}, {"start": 1.7, "end": 9.3}], source_duration=10)
    t = 2.345678
    for _ in range(1000):
        p = tm.to_program(t)
        t = tm.to_source(p)
    assert t == pytest.approx(2.345678, abs=1e-6)


def test_clamp_to_program_intersects_cut_intervals():
    tm = TimeMap([{"start": 2.0, "end": 5.0}, {"start": 8.0, "end": 10.0}])
    # Spans the removed middle: survives as [1.0, 4.0] in program time.
    assert tm.clamp_to_program(3.0, 9.0) == (pytest.approx(1.0), pytest.approx(4.0))
    # Entirely cut.
    assert tm.clamp_to_program(5.5, 7.5) is None
