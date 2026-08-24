from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Protocol


TIMECODE_PATTERN = re.compile(
    r"^\s*(?P<hour>\d{1,2}):(?P<minute>\d{2}):(?P<second>\d{2})"
    r"(?P<separator>[:;])(?P<frame>\d{2})\s*$"
)


class TimingProbe(Protocol):
    path: str
    fps: float
    frame_count: int | None
    timecode: str | None


@dataclass(frozen=True)
class TimecodeAlignmentItem:
    path: str
    timecode: str
    frame_count: int
    skip_frames: int
    available_frames: int

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class TimecodeAlignment:
    fps: float
    timeline_timecode: str
    common_frames: int
    duration_seconds: float
    inputs: tuple[TimecodeAlignmentItem, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "fps": self.fps,
            "timeline_timecode": self.timeline_timecode,
            "common_frames": self.common_frames,
            "duration_seconds": self.duration_seconds,
            "inputs": [item.to_dict() for item in self.inputs],
        }


def _rate_parts(fps: float, drop_frame: bool) -> tuple[int, int]:
    nominal = int(round(fps))
    if nominal < 1:
        raise ValueError("timecode fps must be positive")
    if not drop_frame:
        return nominal, 0
    if min(abs(fps - 29.97), abs(fps - (30_000 / 1_001))) < 0.0005:
        return 30, 2
    if min(abs(fps - 59.94), abs(fps - (60_000 / 1_001))) < 0.0005:
        return 60, 4
    raise ValueError(f"drop-frame timecode is unsupported at {fps:g} fps")


def timecode_to_frames(value: str, fps: float) -> tuple[int, bool]:
    match = TIMECODE_PATTERN.fullmatch(value)
    if not match:
        raise ValueError(f"invalid SMPTE timecode: {value!r}")
    hour = int(match.group("hour"))
    minute = int(match.group("minute"))
    second = int(match.group("second"))
    frame = int(match.group("frame"))
    drop_frame = match.group("separator") == ";"
    nominal, dropped = _rate_parts(fps, drop_frame)
    if hour > 23 or minute > 59 or second > 59 or frame >= nominal:
        raise ValueError(f"timecode component is out of range: {value!r}")
    if dropped and minute % 10 and second == 0 and frame < dropped:
        raise ValueError(f"invalid dropped-frame label: {value!r}")
    total_minutes = hour * 60 + minute
    result = ((hour * 3600 + minute * 60 + second) * nominal) + frame
    result -= dropped * (total_minutes - total_minutes // 10)
    return result, drop_frame


def frames_per_24_hours(fps: float, drop_frame: bool) -> int:
    nominal, dropped = _rate_parts(fps, drop_frame)
    return nominal * 86_400 - dropped * (1_440 - 144)


def frames_to_timecode(frame_number: int, fps: float, drop_frame: bool) -> str:
    nominal, dropped = _rate_parts(fps, drop_frame)
    frame_number %= frames_per_24_hours(fps, drop_frame)
    if dropped:
        frames_per_ten_minutes = nominal * 600 - dropped * 9
        frames_per_minute = nominal * 60 - dropped
        ten_minute_blocks, remainder = divmod(frame_number, frames_per_ten_minutes)
        frame_number += dropped * 9 * ten_minute_blocks
        if remainder >= dropped:
            frame_number += dropped * ((remainder - dropped) // frames_per_minute)
    hour, remainder = divmod(frame_number, nominal * 3600)
    minute, remainder = divmod(remainder, nominal * 60)
    second, frame = divmod(remainder, nominal)
    separator = ";" if drop_frame else ":"
    return f"{hour:02d}:{minute:02d}:{second:02d}{separator}{frame:02d}"


def align_by_timecode(probes: list[TimingProbe]) -> TimecodeAlignment:
    if not probes:
        raise ValueError("no video inputs were provided")
    fps = probes[0].fps
    if fps <= 0.0:
        raise ValueError("input fps must be positive")
    if any(abs(probe.fps - fps) > 0.001 for probe in probes[1:]):
        raise ValueError("timecode alignment requires matching input frame rates")

    parsed: list[tuple[int, bool]] = []
    for probe in probes:
        if not probe.timecode:
            raise ValueError(f"{probe.path}: embedded SMPTE timecode was not found")
        if probe.frame_count is None or probe.frame_count < 1:
            raise ValueError(f"{probe.path}: clip frame count is unavailable")
        parsed.append(timecode_to_frames(probe.timecode, fps))
    drop_modes = {drop for _, drop in parsed}
    if len(drop_modes) != 1:
        raise ValueError("all inputs must use the same drop-frame timecode mode")

    drop_frame = parsed[0][1]
    day = frames_per_24_hours(fps, drop_frame)
    anchor = parsed[0][0]
    unwrapped: list[int] = []
    for start, _ in parsed:
        delta = (start - anchor) % day
        if delta > day // 2:
            delta -= day
        unwrapped.append(anchor + delta)
    timeline_start = max(unwrapped)

    items: list[TimecodeAlignmentItem] = []
    for probe, start in zip(probes, unwrapped, strict=True):
        skip = timeline_start - start
        assert probe.timecode is not None and probe.frame_count is not None
        available = probe.frame_count - skip
        if available < 1:
            raise ValueError(f"{probe.path}: no frames overlap the common timecode range")
        items.append(
            TimecodeAlignmentItem(
                path=probe.path,
                timecode=probe.timecode,
                frame_count=probe.frame_count,
                skip_frames=skip,
                available_frames=available,
            )
        )
    common_frames = min(item.available_frames for item in items)
    return TimecodeAlignment(
        fps=fps,
        timeline_timecode=frames_to_timecode(timeline_start, fps, drop_frame),
        common_frames=common_frames,
        duration_seconds=common_frames / fps,
        inputs=tuple(items),
    )
