from app.services.audio_enhancement import (
    build_stem_mix_filter,
    sanitize_audio_enhance_settings,
)


def test_audio_enhance_settings_are_bounded_and_defaulted():
    assert sanitize_audio_enhance_settings(
        {"speech": 150, "music": -5, "background": "25", "normalize": False}
    ) == {
        "speech": 1.0,
        "music": 0.0,
        "background": 0.25,
        "normalize": False,
    }
    assert sanitize_audio_enhance_settings({}) == {
        "speech": 0.5,
        "music": 0.1,
        "background": 0.1,
        "normalize": True,
    }


def test_stem_mix_has_independent_speech_music_and_background_paths():
    graph = build_stem_mix_filter(
        {"speech": 0.75, "music": 0.2, "background": 0.1, "normalize": True}
    )
    assert "volume=0.250000[dialogue_dry]" in graph
    assert "volume=0.750000[dialogue_wet]" in graph
    assert "volume=0.100000[room]" in graph
    assert "volume=0.200000[music]" in graph
    assert "weights='1 -1'" in graph
    assert "loudnorm=I=-16:TP=-1.5:LRA=11" in graph
