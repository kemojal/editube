"""The generation-loss plan (§5.3 item 8).

The MP4 chain can re-encode the picture up to eight times. `_encode_plan`
decides, before the first segment encodes, which single pass carries the
delivery CRF — every earlier pass runs near-lossless, so the delivered file
passes through exactly one lossy encode at its own quality. These tests pin
that decision table; the passes themselves already have their own tests.
"""

from app.jobs.rough_cut_export import _INTERMEDIATE_CRF, _encode_plan

STAGE_NAMES = [
    "transitions",
    "tail",
    "below_text",
    "captions",
    "layered",
    "shorts",
    "burn_ins",
]


def _stages(**enabled: bool) -> list[tuple[str, bool]]:
    return [(name, enabled.get(name, False)) for name in STAGE_NAMES]


def test_a_bare_cut_delivers_straight_from_the_segment_encodes():
    plan = _encode_plan(_stages(), final_crf=23, intermediate_crf=10)
    assert plan == {"segments": 23}


def test_the_last_encoding_pass_gets_the_delivery_crf():
    plan = _encode_plan(
        _stages(transitions=True, captions=True, burn_ins=True),
        final_crf=23,
        intermediate_crf=10,
    )
    assert plan == {
        "segments": 10,
        "transitions": 10,
        "captions": 10,
        "burn_ins": 23,
    }


def test_stages_that_do_not_run_never_appear():
    plan = _encode_plan(_stages(captions=True), final_crf=18, intermediate_crf=6)
    assert plan == {"segments": 6, "captions": 18}
    assert "shorts" not in plan and "transitions" not in plan


def test_a_single_downstream_pass_still_moves_segments_to_intermediate():
    plan = _encode_plan(_stages(tail=True), final_crf=23, intermediate_crf=10)
    assert plan["segments"] == 10
    assert plan["tail"] == 23


def test_every_quality_tier_has_an_intermediate_crf():
    assert set(_INTERMEDIATE_CRF) == {"draft", "standard", "high"}
    # Near-lossless means meaningfully below the delivery CRFs (28/23/18) —
    # and the ordering matches the tiers: the higher the delivery quality,
    # the cleaner its intermediates.
    assert _INTERMEDIATE_CRF["high"] < _INTERMEDIATE_CRF["standard"] < _INTERMEDIATE_CRF["draft"]
    assert _INTERMEDIATE_CRF["standard"] <= 12
