from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from dataclasses import asdict, dataclass

import imageio_ffmpeg
import numpy as np

from .config import Camera, Color, Video


def ffmpeg_executable() -> str:
    override = os.environ.get("VPSTITCH_FFMPEG", "").strip()
    if override:
        return override

    try:
        bundled = imageio_ffmpeg.get_ffmpeg_exe()
    except (OSError, RuntimeError):
        bundled = ""
    if bundled and Path(bundled).is_file() and os.access(bundled, os.X_OK):
        return bundled

    system = shutil.which("ffmpeg")
    if system:
        return system
    raise FileNotFoundError(
        "FFmpeg was not found. Reinstall imageio-ffmpeg or set VPSTITCH_FFMPEG "
        "to an ffmpeg executable."
    )


@dataclass(frozen=True)
class VideoProbe:
    path: str
    codec: str
    pixel_format: str
    width: int
    height: int
    fps: float
    bit_depth: int
    color_range: str | None
    color_primaries: str | None
    color_trc: str | None
    colorspace: str | None

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def pixel_format_bit_depth(pixel_format: str) -> int:
    packed_rgb = {
        "rgb24": 8,
        "bgr24": 8,
        "rgb48le": 16,
        "rgb48be": 16,
        "rgba64le": 16,
        "rgba64be": 16,
    }
    if pixel_format in packed_rgb:
        return packed_rgb[pixel_format]
    match = re.search(r"(?:p|gbrp|gray)(10|12|14|16)(?:le|be)?$", pixel_format)
    return int(match.group(1)) if match else 8


def parse_probe_output(path: str | Path, text: str) -> VideoProbe:
    candidates = [line.strip() for line in text.splitlines() if " Video: " in line]
    if not candidates:
        raise OSError(f"no video stream found: {path}")
    line = candidates[0]
    codec_match = re.search(r"Video:\s*([^,\s]+)", line)
    pixel_match = re.search(
        r",\s*((?:yuv|yuva|gbr|gray|rgb|bgr)[a-z0-9_]+)(?:\([^)]*\))?",
        line,
    )
    size_match = re.search(r"(?:,|\s)(\d{2,6})x(\d{2,6})(?:[\s,])", line)
    fps_match = re.search(r"([0-9]+(?:\.[0-9]+)?)\s+fps", line)
    if not (codec_match and pixel_match and size_match and fps_match):
        raise OSError(f"unable to parse video stream metadata: {line}")
    pixel_format = pixel_match.group(1)
    parenthetical = ""
    color_match = re.search(re.escape(pixel_format) + r"\(([^)]*)\)", line)
    if color_match:
        parenthetical = color_match.group(1)
    tokens = {token.strip() for token in parenthetical.split(",") if token.strip()}
    color_range = next((v for v in ("tv", "pc") if v in tokens), None)
    descriptor = next((v for v in tokens if "/" in v), None)
    matrix = primaries = transfer = None
    if descriptor:
        parts = descriptor.split("/")
        if len(parts) == 3:
            matrix, primaries, transfer = parts
            matrix = None if matrix in {"gbr", "unknown", "unspecified"} else matrix
            primaries = None if primaries in {"unknown", "unspecified"} else primaries
            transfer = None if transfer in {"unknown", "unspecified"} else transfer
    else:
        shorthand = next(
            (v for v in tokens if v.startswith(("bt", "smpte")) or v == "iec61966-2-1"),
            None,
        )
        if shorthand:
            matrix = primaries = transfer = shorthand
    return VideoProbe(
        path=str(path),
        codec=codec_match.group(1),
        pixel_format=pixel_format,
        width=int(size_match.group(1)),
        height=int(size_match.group(2)),
        fps=float(fps_match.group(1)),
        bit_depth=pixel_format_bit_depth(pixel_format),
        color_range=color_range,
        color_primaries=primaries,
        color_trc=transfer,
        colorspace=matrix,
    )


def probe_video(path: str | Path) -> VideoProbe:
    process = subprocess.run(
        [ffmpeg_executable(), "-hide_banner", "-i", str(path)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        check=False,
    )
    return parse_probe_output(path, process.stderr.decode("utf-8", errors="replace"))


def _read_exact(stream, size: int) -> bytearray | None:
    buffer = bytearray(size)
    view = memoryview(buffer)
    offset = 0
    while offset < size:
        count = stream.readinto(view[offset:])
        if not count:
            if offset == 0:
                return None
            raise OSError(f"truncated raw frame: {offset} of {size} bytes")
        offset += count
    return buffer


class VideoDecoder:
    def __init__(
        self,
        path: str | Path,
        camera: Camera,
        fps: float,
        start_time: float | None = None,
    ):
        self.camera = camera
        self.frame_bytes = camera.width * camera.height * 3 * 2
        command = [
            ffmpeg_executable(),
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(path),
        ]
        if start_time is not None:
            if start_time < 0.0:
                raise ValueError("decoder start_time cannot be negative")
            # Output-side seeking is frame-accurate. This is slower on long-GOP
            # media but reference extraction must favor synchronization.
            command.extend(["-ss", f"{start_time:.9f}"])
        command.extend([
            "-an",
            "-sn",
            "-vf",
            f"fps={fps}",
            "-f",
            "rawvideo",
            "-pix_fmt",
            "rgb48le",
            "pipe:1",
        ])
        self.process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=8 * 1024 * 1024,
        )
        for _ in range(max(camera.frame_offset, 0)):
            self.read()

    def read(self) -> np.ndarray | None:
        assert self.process.stdout is not None
        data = _read_exact(self.process.stdout, self.frame_bytes)
        if data is None:
            return None
        return np.frombuffer(data, dtype="<u2").reshape(
            self.camera.height, self.camera.width, 3
        )

    def close(self) -> None:
        if self.process.stdout:
            self.process.stdout.close()
        self.process.terminate()
        self.process.wait(timeout=10)


def _metadata_args(video: Video) -> list[str]:
    result: list[str] = []
    for option, value in {
        "-color_primaries": video.color_primaries,
        "-color_trc": video.color_trc,
        "-colorspace": video.colorspace,
        "-color_range": video.color_range,
    }.items():
        if value:
            result.extend([option, value])
    return result


def _filter_chain(video: Video) -> str:
    # The encoder input is always full-range RGB48, regardless of the source
    # clip's original YUV range. Tell zscale both sides explicitly so RGB to
    # YUV conversion remains deterministic even when metadata is absent.
    zscale_parameters = ["matrixin=gbr", "rangein=full", "dither=error_diffusion"]
    matrix_names = {
        "bt709": "709",
        "bt2020nc": "2020_ncl",
        "bt2020c": "2020_cl",
        "smpte170m": "170m",
    }
    zscale_parameters.append(f"matrix={matrix_names.get(video.colorspace, '709')}")
    zscale_parameters.append(
        "range="
        + ({"tv": "limited", "pc": "full"}.get(video.color_range, video.color_range)
           if video.color_range
           else "limited")
    )
    filters = ["zscale=" + ":".join(zscale_parameters)]
    parameters: list[str] = []
    if video.color_range:
        parameters.append(
            "range=" + ({"tv": "limited", "pc": "full"}.get(video.color_range, video.color_range))
        )
    if video.color_primaries:
        parameters.append(f"color_primaries={video.color_primaries}")
    if video.color_trc:
        parameters.append(f"color_trc={video.color_trc}")
    if video.colorspace:
        parameters.append(f"colorspace={video.colorspace}")
    if parameters:
        filters.append("setparams=" + ":".join(parameters))
    return ",".join(filters)


class VideoEncoder:
    def __init__(
        self,
        path: str | Path,
        width: int,
        height: int,
        video: Video,
    ):
        if video.output_codec == "hevc-444-10" and width * height > 35_651_584:
            raise ValueError(
                "hevc-444-10 output exceeds the largest standard HEVC picture level; "
                "use ffv1-16, tiff16-sequence, exr-half-sequence, or ProRes"
            )
        command = [
            ffmpeg_executable(),
            "-hide_banner",
            "-loglevel",
            "warning",
            "-y",
            "-f",
            "rawvideo",
            "-pix_fmt",
            "rgb48le",
            "-s:v",
            f"{width}x{height}",
            "-r",
            str(video.fps),
            "-i",
            "pipe:0",
            "-an",
        ]
        if video.output_codec == "ffv1-16":
            command.extend(
                ["-c:v", "ffv1", "-level", "3", "-coder", "1", "-pix_fmt", "gbrp16le"]
            )
        elif video.output_codec == "prores-4444":
            command.extend(
                [
                    "-vf",
                    _filter_chain(video),
                    "-c:v",
                    "prores_ks",
                    "-profile:v",
                    "4",
                    "-pix_fmt",
                    "yuv444p10le",
                ]
            )
        elif video.output_codec == "prores-hq":
            command.extend(
                [
                    "-vf",
                    _filter_chain(video),
                    "-c:v",
                    "prores_ks",
                    "-profile:v",
                    "3",
                    "-pix_fmt",
                    "yuv422p10le",
                ]
            )
        elif video.output_codec == "hevc-444-10":
            command.extend(
                [
                    "-vf",
                    _filter_chain(video),
                    "-c:v",
                    "libx265",
                    "-preset",
                    "slow",
                    "-crf",
                    "12",
                    "-pix_fmt",
                    "yuv444p10le",
                ]
            )
        elif video.output_codec == "h264-mp4-10":
            command.extend(
                [
                    "-vf",
                    _filter_chain(video),
                    "-c:v",
                    "libx264",
                    "-preset",
                    "slow",
                    "-crf",
                    "14",
                    "-pix_fmt",
                    "yuv420p10le",
                    "-movflags",
                    "+faststart",
                ]
            )
        else:
            raise ValueError(f"unsupported output codec: {video.output_codec}")
        command.extend(_metadata_args(video))
        if video.output_codec.startswith("prores"):
            command.extend(["-movflags", "+write_colr"])
        command.append(str(path))
        self.process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=8 * 1024 * 1024,
        )

    def write(self, frame: np.ndarray) -> None:
        assert self.process.stdin is not None
        if frame.dtype != np.uint16 or frame.ndim != 3 or frame.shape[2] != 3:
            raise ValueError("encoder expects uint16 RGB frames")
        contiguous = np.ascontiguousarray(frame)
        if contiguous.dtype.byteorder not in {"<", "=", "|"}:
            contiguous = contiguous.byteswap().view(contiguous.dtype.newbyteorder("<"))
        self.process.stdin.write(memoryview(contiguous).cast("B"))

    def close(self) -> None:
        assert self.process.stdin is not None
        try:
            self.process.stdin.close()
        except BrokenPipeError:
            pass
        stderr = self.process.stderr.read().decode("utf-8", errors="replace") if self.process.stderr else ""
        return_code = self.process.wait()
        if return_code:
            raise OSError(f"ffmpeg encode failed ({return_code}): {stderr}")


class DpxSequenceEncoder:
    """Writes a 12-bit RGB DPX sequence through FFmpeg."""

    def __init__(
        self,
        directory: str | Path,
        width: int,
        height: int,
        video: Video,
        color: Color,
    ):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.width = width
        self.height = height
        self.video = video
        self.color = color
        self.frame_index = 0
        if any(self.directory.glob("frame_*.dpx")):
            raise OSError(f"refusing to overwrite existing DPX sequence: {self.directory}")
        pattern = self.directory / "frame_%06d.dpx"
        command = [
            ffmpeg_executable(),
            "-hide_banner",
            "-loglevel",
            "warning",
            "-n",
            "-f",
            "rawvideo",
            "-pix_fmt",
            "rgb48le",
            "-s:v",
            f"{width}x{height}",
            "-r",
            str(video.fps),
            "-i",
            "pipe:0",
            "-an",
            "-vf",
            "zscale=dither=error_diffusion,format=gbrp12le",
            "-c:v",
            "dpx",
            "-pix_fmt",
            "gbrp12le",
            "-start_number",
            "0",
            str(pattern),
        ]
        self.process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=8 * 1024 * 1024,
        )

    def write(self, frame: np.ndarray) -> None:
        expected = (self.height, self.width, 3)
        if frame.dtype != np.uint16 or frame.shape != expected:
            raise ValueError(f"DPX sequence expects uint16 RGB frames with shape {expected}")
        assert self.process.stdin is not None
        contiguous = np.ascontiguousarray(frame)
        self.process.stdin.write(memoryview(contiguous).cast("B"))
        self.frame_index += 1

    def close(self) -> None:
        assert self.process.stdin is not None
        try:
            self.process.stdin.close()
        except BrokenPipeError:
            pass
        stderr = (
            self.process.stderr.read().decode("utf-8", errors="replace")
            if self.process.stderr
            else ""
        )
        return_code = self.process.wait()
        if return_code:
            raise OSError(f"ffmpeg DPX encode failed ({return_code}): {stderr}")
        manifest = {
            "format": "gbrp12le-dpx-sequence",
            "width": self.width,
            "height": self.height,
            "frames": self.frame_index,
            "fps": self.video.fps,
            "color": asdict(self.color),
            "video_color_tags": {
                "color_primaries": self.video.color_primaries,
                "color_trc": self.video.color_trc,
                "colorspace": self.video.colorspace,
                "color_range": self.video.color_range,
            },
        }
        (self.directory / "vpstitch_manifest.json").write_text(
            json.dumps(manifest, indent=2), encoding="utf-8"
        )
