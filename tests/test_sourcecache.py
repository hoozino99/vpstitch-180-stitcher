from __future__ import annotations

import json
from pathlib import Path

from vpstitch.sourcecache import (
    SOURCE_PROXY_VERSION,
    finalize_source_proxy,
    plan_source_proxy,
    source_proxy_command,
    source_proxy_commands,
    source_proxy_key,
    source_proxy_ready,
)


def test_source_proxy_key_tracks_source_and_preview_dimensions(tmp_path: Path) -> None:
    source = tmp_path / "plate.mov"
    source.write_bytes(b"first")
    original = source_proxy_key(source)

    source.write_bytes(b"changed source bytes")

    assert source_proxy_key(source) != original
    assert source_proxy_key(source, max_width=640) != source_proxy_key(source)


def test_source_proxy_command_is_bounded_and_frame_passthrough(tmp_path: Path) -> None:
    source = tmp_path / "plate.mov"
    source.touch()
    plan = plan_source_proxy(source, tmp_path / "cache")

    _program, arguments = source_proxy_command(plan)

    assert str(source) in arguments
    assert str(plan.temporary) == arguments[-1]
    assert "scale=960:540:force_original_aspect_ratio=decrease:force_divisible_by=2" in arguments
    assert arguments[arguments.index("-pix_fmt") + 1] == "yuv420p"
    assert arguments[arguments.index("-fps_mode") + 1] == "passthrough"
    assert arguments[arguments.index("-progress") + 1] == "pipe:1"


def test_proxy_candidates_prefer_macos_hardware_then_software(tmp_path: Path) -> None:
    source = tmp_path / "plate.mov"
    source.touch()
    plan = plan_source_proxy(source, tmp_path / "cache")

    commands = source_proxy_commands(
        plan,
        platform="darwin",
        encoders={"h264_videotoolbox", "libx264"},
    )

    assert [command.encoder for command in commands] == [
        "h264_videotoolbox",
        "libx264",
    ]
    hardware = list(commands[0].arguments)
    assert hardware[hardware.index("-c:v") + 1] == "h264_videotoolbox"
    assert "-crf" not in hardware


def test_proxy_candidates_filter_unavailable_windows_encoders(tmp_path: Path) -> None:
    source = tmp_path / "plate.mov"
    source.touch()
    plan = plan_source_proxy(source, tmp_path / "cache")

    commands = source_proxy_commands(
        plan,
        platform="win32",
        encoders={"h264_qsv", "libx264"},
    )

    assert [command.encoder for command in commands] == ["h264_qsv", "libx264"]


def test_finalize_source_proxy_is_atomic_and_validates_metadata(tmp_path: Path) -> None:
    source = tmp_path / "plate.mov"
    source.touch()
    plan = plan_source_proxy(source, tmp_path / "cache")
    source_proxy_command(plan)
    plan.temporary.write_bytes(b"proxy")

    output = finalize_source_proxy(plan)

    assert output.read_bytes() == b"proxy"
    assert source_proxy_ready(plan)
    metadata = json.loads(plan.metadata.read_text(encoding="utf-8"))
    assert metadata["version"] == SOURCE_PROXY_VERSION
    assert metadata["key"] == plan.key

    metadata["key"] = "stale"
    plan.metadata.write_text(json.dumps(metadata), encoding="utf-8")
    assert not source_proxy_ready(plan)
