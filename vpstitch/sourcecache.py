from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import uuid
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from .ffmpegio import ffmpeg_executable


SOURCE_PROXY_VERSION = 1


@dataclass(frozen=True, slots=True)
class SourceProxyPlan:
    source: Path
    key: str
    output: Path
    temporary: Path
    metadata: Path
    max_width: int
    max_height: int


@dataclass(frozen=True, slots=True)
class SourceProxyCommand:
    encoder: str
    program: str
    arguments: tuple[str, ...]


def source_proxy_key(
    source: str | Path,
    *,
    max_width: int = 960,
    max_height: int = 540,
) -> str:
    path = Path(source).resolve()
    stat = path.stat()
    payload = {
        "version": SOURCE_PROXY_VERSION,
        "path": os.path.normcase(str(path)),
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "max_width": max_width,
        "max_height": max_height,
        "codec": "h264-yuv420p-crf22",
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    return hashlib.sha256(encoded).hexdigest()[:24]


def plan_source_proxy(
    source: str | Path,
    cache_root: str | Path,
    *,
    max_width: int = 960,
    max_height: int = 540,
) -> SourceProxyPlan:
    if max_width < 2 or max_height < 2:
        raise ValueError("source proxy dimensions must be at least 2x2")
    source_path = Path(source).resolve()
    if not source_path.is_file():
        raise FileNotFoundError(source_path)
    key = source_proxy_key(
        source_path,
        max_width=max_width,
        max_height=max_height,
    )
    directory = Path(cache_root) / "source-proxies" / key[:2]
    output = directory / f"{key}.mp4"
    build_token = f"{os.getpid()}-{uuid.uuid4().hex[:10]}"
    return SourceProxyPlan(
        source=source_path,
        key=key,
        output=output,
        temporary=directory / f".{key}.{build_token}.part.mp4",
        metadata=directory / f"{key}.json",
        max_width=max_width,
        max_height=max_height,
    )


def source_proxy_ready(plan: SourceProxyPlan) -> bool:
    if not plan.output.is_file() or plan.output.stat().st_size < 1:
        return False
    try:
        metadata = json.loads(plan.metadata.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return (
        metadata.get("version") == SOURCE_PROXY_VERSION
        and metadata.get("key") == plan.key
        and metadata.get("source") == str(plan.source)
    )


@lru_cache(maxsize=4)
def available_proxy_encoders(program: str | None = None) -> frozenset[str]:
    executable = program or ffmpeg_executable()
    try:
        process = subprocess.run(
            [executable, "-hide_banner", "-encoders"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return frozenset({"libx264"})
    result: set[str] = set()
    for line in process.stdout.decode("utf-8", errors="replace").splitlines():
        fields = line.split()
        if len(fields) >= 2 and fields[0].startswith("V"):
            result.add(fields[1])
    result.add("libx264")
    return frozenset(result)


def preferred_proxy_encoders(platform: str | None = None) -> tuple[str, ...]:
    target = platform or sys.platform
    if target == "darwin":
        return ("h264_videotoolbox", "libx264")
    if target.startswith("win"):
        return ("h264_nvenc", "h264_qsv", "h264_amf", "libx264")
    return ("h264_vaapi", "h264_qsv", "libx264")


def source_proxy_command(
    plan: SourceProxyPlan,
    encoder: str = "libx264",
) -> tuple[str, list[str]]:
    plan.output.parent.mkdir(parents=True, exist_ok=True)
    plan.temporary.unlink(missing_ok=True)
    scale = (
        f"scale={plan.max_width}:{plan.max_height}:"
        "force_original_aspect_ratio=decrease:force_divisible_by=2"
    )
    arguments = [
        "-hide_banner",
        "-y",
        "-i",
        str(plan.source),
        "-map",
        "0:v:0",
        "-map_metadata",
        "0",
        "-an",
        "-sn",
        "-vf",
        scale,
        "-c:v",
        encoder,
    ]
    if encoder == "libx264":
        arguments.extend(["-preset", "veryfast", "-crf", "22"])
    else:
        # These generic bitrate controls are supported by the macOS and
        # Windows hardware H.264 encoders. Backend-specific speed hints are
        # optional; a runtime failure falls through to the next candidate.
        arguments.extend(
            ["-b:v", "1200k", "-maxrate", "2400k", "-bufsize", "3600k"]
        )
        if encoder == "h264_videotoolbox":
            arguments.extend(["-prio_speed", "1"])
        elif encoder == "h264_nvenc":
            arguments.extend(["-preset", "p4"])
        elif encoder == "h264_qsv":
            arguments.extend(["-preset", "veryfast"])
        elif encoder == "h264_amf":
            arguments.extend(["-quality", "speed"])
    arguments.extend([
        "-pix_fmt",
        "yuv420p",
        "-fps_mode",
        "passthrough",
        "-movflags",
        "+faststart",
        "-threads",
        "2",
        "-progress",
        "pipe:1",
        "-nostats",
        str(plan.temporary),
    ])
    return ffmpeg_executable(), arguments


def source_proxy_commands(
    plan: SourceProxyPlan,
    *,
    platform: str | None = None,
    encoders: frozenset[str] | set[str] | None = None,
) -> tuple[SourceProxyCommand, ...]:
    # Do not synchronously enumerate ffmpeg encoders from the UI thread. The
    # queue already retries each candidate after a runtime failure.
    available = None if encoders is None else frozenset(encoders)
    commands: list[SourceProxyCommand] = []
    for encoder in preferred_proxy_encoders(platform):
        if available is not None and encoder not in available and encoder != "libx264":
            continue
        program, arguments = source_proxy_command(plan, encoder)
        commands.append(SourceProxyCommand(encoder, program, tuple(arguments)))
    if not commands:
        program, arguments = source_proxy_command(plan, "libx264")
        commands.append(SourceProxyCommand("libx264", program, tuple(arguments)))
    return tuple(commands)


def finalize_source_proxy(plan: SourceProxyPlan, *, encoder: str = "unknown") -> Path:
    if not plan.temporary.is_file() or plan.temporary.stat().st_size < 1:
        raise OSError(f"source proxy output is missing: {plan.temporary}")
    plan.temporary.replace(plan.output)
    payload = {
        "version": SOURCE_PROXY_VERSION,
        "key": plan.key,
        "source": str(plan.source),
        "output": str(plan.output),
        "max_width": plan.max_width,
        "max_height": plan.max_height,
        "encoder": encoder,
    }
    temporary_metadata = plan.metadata.with_name(
        f".{plan.metadata.name}.{os.getpid()}-{uuid.uuid4().hex[:10]}.part"
    )
    temporary_metadata.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    temporary_metadata.replace(plan.metadata)
    return plan.output
