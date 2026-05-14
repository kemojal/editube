from app.jobs.rough_cut_effect import build_ffmpeg_effect_command


def test_build_speed_effect_command_splits_video_and_audio_speed():
    cmd = build_ffmpeg_effect_command(
        "input.mp4",
        "out.mp4",
        effect_type="speed",
        clip_target={"track": "video", "start": 2, "end": 8},
        settings={"rate": 2},
    )

    assert "-ss" in cmd
    assert "2.000" in cmd
    assert "-t" in cmd
    assert "6.000" in cmd
    assert "-vf" in cmd
    assert "setpts=PTS/2.00000" in cmd
    assert "-af" in cmd
    assert "atempo=2.00000" in cmd


def test_build_audio_effect_command_uses_volume_and_fades():
    cmd = build_ffmpeg_effect_command(
        "input.mp4",
        "out.mp4",
        effect_type="audio",
        clip_target={"track": "audio", "start": 10, "end": 14},
        settings={"volume": -3, "fadeIn": 0.5, "fadeOut": 1},
    )

    af = cmd[cmd.index("-af") + 1]
    assert "volume=-3.000dB" in af
    assert "afade=t=in:st=0:d=0.500" in af
    assert "afade=t=out:st=3.000:d=1.000" in af
