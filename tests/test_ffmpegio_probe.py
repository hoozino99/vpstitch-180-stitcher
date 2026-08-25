from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

import vpstitch.ffmpegio as ffmpegio


FFMPEG_PROBE_TEXT = b"""
  Duration: 00:00:10.00, start: 0.000000, bitrate: 1234 kb/s
    timecode        : 01:00:00:00
  Stream #0:0: Video: h264, yuv420p(tv, bt709), 1920x1080, 24 fps
"""


def _ffprobe_payload(
    *, format_duration: object = "10.0", **video_overrides: object
) -> bytes:
    video: dict[str, object] = {
        "codec_type": "video",
        "codec_name": "h264",
        "pix_fmt": "yuv420p",
        "width": 1920,
        "height": 1080,
        "avg_frame_rate": "24/1",
        "r_frame_rate": "24/1",
        "duration": "10.0",
        "duration_ts": "240000",
        "time_base": "1/24000",
        "nb_frames": "240",
        "tags": {"timecode": "01:00:00:00"},
    }
    video.update(video_overrides)
    return json.dumps(
        {"streams": [video], "format": {"duration": format_duration}}
    ).encode()


def _install_probe_process(
    monkeypatch: pytest.MonkeyPatch, payload: bytes
) -> None:
    monkeypatch.setattr(ffmpegio, "ffmpeg_executable", lambda: "ffmpeg")
    monkeypatch.setattr(ffmpegio, "ffprobe_executable", lambda: "ffprobe")

    def fake_run(command: list[str], **_: object) -> subprocess.CompletedProcess[bytes]:
        if command[0] == "ffmpeg":
            return subprocess.CompletedProcess(command, 1, b"", FFMPEG_PROBE_TEXT)
        if command[0] == "ffprobe":
            return subprocess.CompletedProcess(command, 0, payload, b"")
        raise AssertionError(f"unexpected executable: {command[0]}")

    monkeypatch.setattr(ffmpegio.subprocess, "run", fake_run)


def test_count_frames_uses_validated_nb_frames_without_decode(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _install_probe_process(monkeypatch, _ffprobe_payload())
    monkeypatch.setattr(
        ffmpegio,
        "_count_video_frames",
        lambda path: pytest.fail(f"unexpected frame decode: {path}"),
    )

    probe = ffmpegio.probe_video(tmp_path / "take.mov", count_frames=True)

    assert probe.frame_count == 240


def test_count_frames_uses_integral_duration_when_nb_frames_is_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _install_probe_process(monkeypatch, _ffprobe_payload(nb_frames="N/A"))
    monkeypatch.setattr(
        ffmpegio,
        "_count_video_frames",
        lambda path: pytest.fail(f"unexpected frame decode: {path}"),
    )

    probe = ffmpegio.probe_video(tmp_path / "take.mov", count_frames=True)

    assert probe.frame_count == 240


@pytest.mark.parametrize(
    "video_overrides",
    [
        {"nb_frames": "200"},
        {"avg_frame_rate": "24000/1001", "r_frame_rate": "24/1"},
        {"duration": "10.0", "duration_ts": "239000"},
        {"nb_frames": "N/A", "duration": "10.02", "duration_ts": "N/A"},
        {
            "nb_frames": "N/A",
            "duration": "N/A",
            "duration_ts": "N/A",
            "format_duration": "N/A",
        },
    ],
)
def test_count_frames_decodes_when_metadata_is_ambiguous_or_missing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    video_overrides: dict[str, object],
) -> None:
    overrides = dict(video_overrides)
    format_duration = overrides.pop("format_duration", "10.0")
    _install_probe_process(
        monkeypatch,
        _ffprobe_payload(format_duration=format_duration, **overrides),
    )
    decoded: list[Path] = []

    def count_frames(path: str | Path) -> int:
        decoded.append(Path(path))
        return 241

    monkeypatch.setattr(ffmpegio, "_count_video_frames", count_frames)
    source = tmp_path / "take.mov"

    probe = ffmpegio.probe_video(source, count_frames=True)

    assert probe.frame_count == 241
    assert decoded == [source]
