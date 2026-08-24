from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path

import pytest

from vpstitch.cli import _planned_decoder_starts
from vpstitch.config import Camera, Lens, RigConfig, Video
from vpstitch.timecode import (
    align_by_timecode,
    frames_to_timecode,
    timecode_to_frames,
)


@dataclass(frozen=True)
class Probe:
    path: str
    fps: float
    frame_count: int | None
    timecode: str | None


def test_non_drop_timecode_roundtrip() -> None:
    frames, drop = timecode_to_frames("01:02:03:12", 24.0)
    assert not drop
    assert frames_to_timecode(frames, 24.0, drop) == "01:02:03:12"


def test_drop_frame_timecode_roundtrip() -> None:
    frames, drop = timecode_to_frames("01:00:00;00", 29.97)
    assert drop
    assert frames == 107_892
    assert frames_to_timecode(frames, 29.97, drop) == "01:00:00;00"


def test_drop_frame_rejects_skipped_label() -> None:
    with pytest.raises(ValueError, match="dropped-frame label"):
        timecode_to_frames("00:01:00;01", 29.97)


def test_alignment_uses_latest_start_and_shortest_tail() -> None:
    result = align_by_timecode(
        [
            Probe("cam0.mov", 24.0, 100, "01:00:00:00"),
            Probe("cam1.mov", 24.0, 99, "01:00:00:02"),
            Probe("cam2.mov", 24.0, 98, "01:00:00:01"),
        ]
    )
    assert result.timeline_timecode == "01:00:00:02"
    assert [item.skip_frames for item in result.inputs] == [2, 0, 1]
    assert result.common_frames == 97


def test_alignment_handles_midnight_rollover() -> None:
    result = align_by_timecode(
        [
            Probe("cam0.mov", 24.0, 100, "23:59:59:23"),
            Probe("cam1.mov", 24.0, 100, "00:00:00:01"),
        ]
    )
    assert result.timeline_timecode == "00:00:00:01"
    assert [item.skip_frames for item in result.inputs] == [2, 0]
    assert result.common_frames == 98


def test_alignment_requires_embedded_timecode() -> None:
    with pytest.raises(ValueError, match="timecode was not found"):
        align_by_timecode([Probe("cam0.mov", 24.0, 100, None)])


def test_render_plan_combines_tc_skip_and_manual_offset(tmp_path: Path) -> None:
    sources = [str(tmp_path / "cam0.mov"), str(tmp_path / "cam1.mov")]
    plan = {
        "fps": 24.0,
        "common_frames": 8,
        "inputs": [
            {"path": sources[0], "skip_frames": 2, "frame_count": 10},
            {"path": sources[1], "skip_frames": 0, "frame_count": 10},
        ],
    }
    plan_path = tmp_path / "alignment.json"
    plan_path.write_text(json.dumps(plan), encoding="utf-8")
    lens = Lens("pinhole", 10, 10, 5, 5)
    config = RigConfig(
        cameras=(
            Camera("cam0", 10, 10, -1, 0, 0, lens, frame_offset=1),
            Camera("cam1", 10, 10, 1, 0, 0, lens, frame_offset=0),
        ),
        video=Video(fps=24.0),
    )
    starts, available = _planned_decoder_starts(config, sources, str(plan_path))
    assert starts == [3, 0]
    assert available == 7
