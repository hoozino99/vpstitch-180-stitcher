from __future__ import annotations

import pytest

from vpstitch.config import ConfigError, Video
from vpstitch.diagnostics import assess_inputs, resolve_passthrough_video
from vpstitch.ffmpegio import VideoProbe, parse_probe_output, pixel_format_bit_depth


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


def test_parse_ffmpeg_hlg_metadata() -> None:
    text = """
      Stream #0:0: Video: prores (HQ), yuv422p10le(tv, bt2020nc/bt2020/arib-std-b67, progressive), 5952x3968, 29.97 fps
    """
    probe = parse_probe_output("cam0.mov", text)
    assert probe.bit_depth == 10
    assert probe.color_range == "tv"
    assert probe.colorspace == "bt2020nc"
    assert probe.color_primaries == "bt2020"
    assert probe.color_trc == "arib-std-b67"


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
