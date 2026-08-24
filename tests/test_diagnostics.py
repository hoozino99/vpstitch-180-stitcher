from __future__ import annotations

import pytest

from vpstitch.config import Camera, ConfigError, Lens, RigConfig, Video
from vpstitch.diagnostics import (
    assess_inputs,
    interpret_input_probes,
    resolve_passthrough_video,
)
from vpstitch.ffmpegio import (
    VideoProbe,
    ffmpeg_executable,
    parse_probe_output,
    pixel_format_bit_depth,
)


def _probe(path: str, depth: int = 10) -> VideoProbe:
    return VideoProbe(
        path=path,
        codec="prores",
        pixel_format=f"yuv422p{depth}le",
        width=5952,
        height=3968,
        fps=29.97,
        bit_depth=depth,
        color_range="tv",
        color_primaries="bt2020",
        color_trc="arib-std-b67",
        colorspace="bt2020nc",
    )


def test_pixel_format_depth() -> None:
    assert pixel_format_bit_depth("yuv422p10le") == 10
    assert pixel_format_bit_depth("gbrp16le") == 16
    assert pixel_format_bit_depth("yuv420p") == 8
    assert pixel_format_bit_depth("rgb48le") == 16


def test_ffmpeg_environment_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VPSTITCH_FFMPEG", "/custom/bin/ffmpeg")
    assert ffmpeg_executable() == "/custom/bin/ffmpeg"


def test_parse_ffmpeg_hlg_metadata() -> None:
    text = """
      Duration: 00:00:10.01, start: 0.000000, bitrate: 1234 kb/s
        timecode        : 01:00:00;00
      Stream #0:0: Video: prores (HQ), yuv422p10le(tv, bt2020nc/bt2020/arib-std-b67, progressive), 5952x3968, 29.97 fps
    """
    probe = parse_probe_output("cam0.mov", text)
    assert probe.bit_depth == 10
    assert probe.color_range == "tv"
    assert probe.colorspace == "bt2020nc"
    assert probe.color_primaries == "bt2020"
    assert probe.color_trc == "arib-std-b67"
    assert probe.duration_seconds == 10.01
    assert probe.frame_count == 300
    assert probe.timecode == "01:00:00;00"


def test_diagnostics_reject_eight_bit_camera() -> None:
    report = assess_inputs([_probe("cam0.mov"), _probe("cam1.mov", depth=8)])
    assert not report.passed
    assert any(issue.code == "input-below-10bit" for issue in report.issues)
    assert any(issue.code == "bit-depth-mismatch" for issue in report.issues)


def test_passthrough_inherits_input_color_tags() -> None:
    resolved = resolve_passthrough_video(Video(fps=24), [_probe("cam.mov")])
    assert resolved.color_primaries == "bt2020"
    assert resolved.color_trc == "arib-std-b67"
    assert resolved.colorspace == "bt2020nc"
    assert resolved.color_range == "tv"


def test_passthrough_rejects_conflicting_retag() -> None:
    with pytest.raises(ConfigError, match="cannot relabel"):
        resolve_passthrough_video(
            Video(fps=24, color_primaries="bt709"), [_probe("cam.mov")]
        )


def test_input_interpretation_overrides_detected_color_metadata() -> None:
    camera = Camera(
        "cam0",
        5952,
        3968,
        0,
        0,
        0,
        Lens("pinhole", 1, 1, 0, 0),
        input_color_space="bt709",
        input_video_range="pc",
    )
    interpreted = interpret_input_probes(
        [_probe("cam.mov")], RigConfig(cameras=(camera,))
    )[0]
    assert interpreted.colorspace == "bt709"
    assert interpreted.color_primaries == "bt709"
    assert interpreted.color_trc == "bt709"
    assert interpreted.color_range == "pc"
