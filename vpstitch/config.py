from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


class ConfigError(ValueError):
    pass


MAX_CANVAS_WIDTH = 20_000
MAX_CANVAS_HEIGHT = 6_000
OUTPUT_CODECS = {
    "ffv1-16",
    "exr-half-sequence",
    "dpx12-sequence",
    "prores-4444",
    "prores-hq",
    "h264-mp4-10",
    "hevc-444-10",
}


@dataclass(frozen=True)
class Lens:
    model: str
    fx: float
    fy: float
    cx: float
    cy: float
    distortion: tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0)
    circle_radius: float | None = None


@dataclass(frozen=True)
class Camera:
    name: str
    width: int
    height: int
    yaw_deg: float
    pitch_deg: float
    roll_deg: float
    lens: Lens
    colorspace: str | None = None
    frame_offset: int = 0


@dataclass(frozen=True)
class Output:
    width: int = 15360
    height: int = 3968
    horizontal_fov_deg: float = 180.0
    vertical_fov_deg: float = 72.0
    center_yaw_deg: float = 0.0
    center_pitch_deg: float = 0.0
    projection: str = "cylindrical"
    # Mild central bulge for the cylindrical_rugby projection. 0.10 makes the
    # center region slightly larger while tapering the left/right regions.
    rugby_strength: float = 0.0
    tile_width: int = 1024
    tile_height: int = 512
    seam_feather_deg: float = 4.0


@dataclass(frozen=True)
class Color:
    mode: str = "passthrough"
    ocio_config: str | None = None
    working_space: str | None = None
    output_space: str | None = None
    integer_dither: bool = True
    dither_seed: int = 7349


@dataclass(frozen=True)
class Flow:
    enabled: bool = False
    algorithm: str = "dis"
    preset: str = "medium"
    confidence_threshold: float = 0.35
    max_displacement_px: float = 32.0


@dataclass(frozen=True)
class Video:
    fps: float
    frames: int | None = None
    output_codec: str = "ffv1-16"
    color_primaries: str | None = None
    color_trc: str | None = None
    colorspace: str | None = None
    color_range: str | None = None


@dataclass(frozen=True)
class RigConfig:
    cameras: tuple[Camera, ...]
    output: Output = field(default_factory=Output)
    color: Color = field(default_factory=Color)
    flow: Flow = field(default_factory=Flow)
    video: Video | None = None


def _expect_dict(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ConfigError(f"{label} must be an object")
    return value


def _lens(raw: dict[str, Any]) -> Lens:
    distortion = tuple(float(x) for x in raw.get("distortion", [0, 0, 0, 0]))
    if len(distortion) != 4:
        raise ConfigError("lens.distortion must contain exactly four values")
    model = str(raw.get("model", "fisheye_equidistant"))
    if model not in {"pinhole", "fisheye_equidistant"}:
        raise ConfigError(f"unsupported lens model: {model}")
    return Lens(
        model=model,
        fx=float(raw["fx"]),
        fy=float(raw.get("fy", raw["fx"])),
        cx=float(raw["cx"]),
        cy=float(raw["cy"]),
        distortion=distortion,  # type: ignore[arg-type]
        circle_radius=(
            None if raw.get("circle_radius") is None else float(raw["circle_radius"])
        ),
    )


def _camera(raw: dict[str, Any]) -> Camera:
    return Camera(
        name=str(raw["name"]),
        width=int(raw["width"]),
        height=int(raw["height"]),
        yaw_deg=float(raw["yaw_deg"]),
        pitch_deg=float(raw.get("pitch_deg", 0.0)),
        roll_deg=float(raw.get("roll_deg", 0.0)),
        lens=_lens(_expect_dict(raw["lens"], "camera.lens")),
        colorspace=raw.get("colorspace"),
        frame_offset=int(raw.get("frame_offset", 0)),
    )


def load_config(path: str | Path) -> RigConfig:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    root = _expect_dict(raw, "config")
    cameras = tuple(_camera(_expect_dict(v, "camera")) for v in root["cameras"])
    if not cameras:
        raise ConfigError("at least one camera is required")
    if len({camera.name for camera in cameras}) != len(cameras):
        raise ConfigError("camera names must be unique")
    if any(
        cameras[index].yaw_deg >= cameras[index + 1].yaw_deg
        for index in range(len(cameras) - 1)
    ):
        raise ConfigError("cameras must be ordered from left to right by increasing yaw_deg")

    out_raw = _expect_dict(root.get("output", {}), "output")
    output = Output(**out_raw)
    if output.projection not in {"cylindrical", "cylindrical_rugby", "rectilinear"}:
        raise ConfigError(
            "projection must be 'cylindrical', 'cylindrical_rugby', or 'rectilinear'"
        )
    if not 0.0 <= output.rugby_strength < 1.0:
        raise ConfigError("rugby_strength must be between 0 (inclusive) and 1 (exclusive)")
    if output.width < 1 or output.height < 1:
        raise ConfigError("output dimensions must be positive")
    if output.width > MAX_CANVAS_WIDTH or output.height > MAX_CANVAS_HEIGHT:
        raise ConfigError(
            f"canvas exceeds the supported {MAX_CANVAS_WIDTH}x{MAX_CANVAS_HEIGHT} maximum"
        )
    if not 0.0 < output.horizontal_fov_deg <= 360.0:
        raise ConfigError("horizontal_fov_deg must be between 0 and 360")
    if not 0.0 < output.vertical_fov_deg < 180.0:
        raise ConfigError("vertical_fov_deg must be between 0 and 180")
    if output.tile_width < 32 or output.tile_height < 32:
        raise ConfigError("tile dimensions must be at least 32 pixels")

    color_raw = _expect_dict(root.get("color", {}), "color")
    color = Color(**color_raw)
    if color.mode not in {"passthrough", "ocio"}:
        raise ConfigError("color.mode must be 'passthrough' or 'ocio'")
    if color.mode == "ocio":
        missing = [
            name
            for name, value in {
                "ocio_config": color.ocio_config,
                "working_space": color.working_space,
                "output_space": color.output_space,
            }.items()
            if not value
        ]
        if missing:
            raise ConfigError(f"OCIO mode requires: {', '.join(missing)}")
        if any(not camera.colorspace for camera in cameras):
            raise ConfigError("OCIO mode requires colorspace on every camera")

    flow_raw = _expect_dict(root.get("flow", {}), "flow")
    flow = Flow(**flow_raw)
    if flow.algorithm != "dis":
        raise ConfigError("version 0.1 supports DIS optical flow only")
    if flow.preset not in {"ultrafast", "fast", "medium"}:
        raise ConfigError("flow.preset must be ultrafast, fast, or medium")
    if not 0.0 <= flow.confidence_threshold <= 1.0:
        raise ConfigError("flow.confidence_threshold must be between 0 and 1")

    video_raw = root.get("video")
    video = None if video_raw is None else Video(**_expect_dict(video_raw, "video"))
    if video is not None:
        if video.fps <= 0.0:
            raise ConfigError("video.fps must be positive")
        if video.frames is not None and video.frames < 1:
            raise ConfigError("video.frames must be positive or null")
        if video.output_codec not in OUTPUT_CODECS:
            raise ConfigError(
                f"video.output_codec must be one of: {', '.join(sorted(OUTPUT_CODECS))}"
            )
        if (
            video.output_codec == "hevc-444-10"
            and output.width * output.height > 35_651_584
        ):
            raise ConfigError(
                "hevc-444-10 canvas exceeds the largest standard HEVC picture level; "
                "use ffv1-16, exr-half-sequence, or ProRes for this canvas"
            )
    return RigConfig(cameras=cameras, output=output, color=color, flow=flow, video=video)
