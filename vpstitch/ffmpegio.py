from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import threading
from pathlib import Path
from dataclasses import asdict, dataclass, replace
from fractions import Fraction

import imageio_ffmpeg
import numpy as np

from .config import Camera, Color, Video


PROBE_TIMEOUT_SECONDS = 15


class _BoundedPipeReader:
    """Drain a subprocess pipe continuously while retaining only recent text."""

    def __init__(self, stream, *, max_bytes: int = 256 * 1024) -> None:  # type: ignore[no-untyped-def]
        self._stream = stream
        self._max_bytes = max(4096, int(max_bytes))
        self._buffer = bytearray()
        self._lock = threading.Lock()
        self._thread = threading.Thread(
            target=self._drain,
            name="vpstitch-ffmpeg-stderr",
            daemon=True,
        )
        self._thread.start()

    def _drain(self) -> None:
        try:
            while True:
                chunk = self._stream.read(8192)
                if not chunk:
                    return
                with self._lock:
                    self._buffer.extend(chunk)
                    overflow = len(self._buffer) - self._max_bytes
                    if overflow > 0:
                        del self._buffer[:overflow]
        except (AttributeError, OSError, ValueError):
            return

    def text(self) -> str:
        with self._lock:
            payload = bytes(self._buffer)
        return payload.decode("utf-8", errors="replace")

    def finish(self, timeout: float = 3.0) -> str:
        self._thread.join(timeout=timeout)
        return self.text()


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
    duration_seconds: float | None = None
    frame_count: int | None = None
    timecode: str | None = None
    bit_rate: int | None = None

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
    duration_match = re.search(
        r"Duration:\s*(\d{1,2}):(\d{2}):(\d{2}(?:\.\d+)?)", text
    )
    duration_seconds = None
    if duration_match:
        duration_seconds = (
            int(duration_match.group(1)) * 3600
            + int(duration_match.group(2)) * 60
            + float(duration_match.group(3))
        )
    timecode_match = re.search(
        r"(?im)^\s*timecode\s*:\s*(\d{1,2}:\d{2}:\d{2}[:;]\d{2})\s*$", text
    )
    bit_rate_match = re.search(
        r"(?i)bitrate:\s*([0-9]+(?:\.[0-9]+)?)\s*k(?:bits?/s|b/s)", text
    )
    fps = float(fps_match.group(1))
    return VideoProbe(
        path=str(path),
        codec=codec_match.group(1),
        pixel_format=pixel_format,
        width=int(size_match.group(1)),
        height=int(size_match.group(2)),
        fps=fps,
        bit_depth=pixel_format_bit_depth(pixel_format),
        color_range=color_range,
        color_primaries=primaries,
        color_trc=transfer,
        colorspace=matrix,
        duration_seconds=duration_seconds,
        frame_count=(
            None
            if duration_seconds is None
            else max(1, int(round(duration_seconds * fps)))
        ),
        timecode=timecode_match.group(1) if timecode_match else None,
        bit_rate=(
            int(round(float(bit_rate_match.group(1)) * 1000))
            if bit_rate_match
            else None
        ),
    )


def ffprobe_executable() -> str | None:
    override = os.environ.get("VPSTITCH_FFPROBE", "").strip()
    if override:
        return override
    ffmpeg = Path(ffmpeg_executable())
    name = "ffprobe.exe" if os.name == "nt" else "ffprobe"
    candidates = []
    if getattr(sys, "frozen", False):
        candidates.append(Path(sys.executable).with_name(name))
    candidates.append(ffmpeg.with_name(name))
    for candidate in candidates:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return shutil.which("ffprobe")


def _fraction_rate(value: object) -> float | None:
    if not isinstance(value, str) or value in {"", "0/0", "N/A"}:
        return None
    try:
        result = float(Fraction(value))
    except (ValueError, ZeroDivisionError):
        return None
    return result if result > 0.0 else None


def _tag_value(tags: object, key: str) -> str | None:
    if not isinstance(tags, dict):
        return None
    for name, value in tags.items():
        if str(name).lower() == key.lower() and value:
            return str(value)
    return None


def _positive_float(value: object) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if np.isfinite(result) and result > 0.0 else None


def _positive_int(value: object) -> int | None:
    try:
        result = int(value)
    except (TypeError, ValueError):
        return None
    return result if result > 0 else None


def _stream_duration_seconds(
    video: dict[str, object],
) -> tuple[float | None, bool]:
    duration = _positive_float(video.get("duration"))
    timestamp_count = _positive_int(video.get("duration_ts"))
    time_base = _fraction_rate(video.get("time_base"))
    timestamp_duration = (
        timestamp_count * time_base
        if timestamp_count is not None and time_base is not None
        else None
    )
    if duration is not None and timestamp_duration is not None:
        if abs(duration - timestamp_duration) > max(1e-6, duration * 1e-6):
            return None, True
    return duration or timestamp_duration, False


def _validated_metadata_frame_count(
    video: dict[str, object], format_data: dict[str, object], fps: float
) -> int | None:
    """Return a frame count only when container metadata is CFR-consistent."""
    if not np.isfinite(fps) or fps <= 0.0:
        return None

    average_rate = _fraction_rate(video.get("avg_frame_rate"))
    nominal_rate = _fraction_rate(video.get("r_frame_rate"))
    if average_rate is not None and nominal_rate is not None:
        rate_tolerance = max(0.001, fps * 1e-4)
        if abs(average_rate - nominal_rate) > rate_tolerance:
            return None

    duration, ambiguous_duration = _stream_duration_seconds(video)
    if ambiguous_duration:
        return None
    if duration is None:
        duration = _positive_float(format_data.get("duration"))
    if duration is None:
        return None

    expected = duration * fps
    rounded = int(round(expected))
    if rounded < 1:
        return None

    declared = _positive_int(video.get("nb_frames"))
    if declared is not None:
        # One frame allows common container end-timestamp rounding while still
        # rejecting stale or stream-mismatched frame-count metadata.
        return declared if abs(declared - expected) <= 1.0 + 1e-6 else None

    # A duration-only count is safe only when it lands very close to a whole
    # frame. Otherwise the source may be VFR or the container duration rounded.
    return rounded if abs(rounded - expected) <= 0.05 else None


def _enhance_probe_with_ffprobe(
    path: str | Path, base: VideoProbe
) -> tuple[VideoProbe, int | None]:
    executable = ffprobe_executable()
    if not executable:
        return base, None
    try:
        process = subprocess.run(
            [
                executable,
                "-v",
                "error",
                "-show_entries",
                "format=duration,start_time,bit_rate:format_tags=timecode:"
                "stream=index,codec_type,codec_name,pix_fmt,width,height,avg_frame_rate,"
                "r_frame_rate,nb_frames,duration,duration_ts,time_base,bit_rate:stream_tags=timecode",
                "-of",
                "json",
                str(path),
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=PROBE_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired):
        return base, None
    if process.returncode:
        return base, None
    try:
        payload = json.loads(process.stdout.decode("utf-8", errors="replace"))
        streams = payload.get("streams", [])
        video = next(stream for stream in streams if stream.get("codec_type") == "video")
        format_data = payload.get("format", {})
    except (json.JSONDecodeError, StopIteration, TypeError, AttributeError):
        return base, None

    fps = (
        _fraction_rate(video.get("avg_frame_rate"))
        or _fraction_rate(video.get("r_frame_rate"))
        or base.fps
    )
    duration_seconds = None
    for candidate in (video.get("duration"), format_data.get("duration")):
        try:
            duration_seconds = float(candidate)
        except (TypeError, ValueError):
            continue
        if duration_seconds >= 0.0:
            break
        duration_seconds = None
    frame_count = _positive_int(video.get("nb_frames"))
    if not frame_count and duration_seconds is not None:
        frame_count = max(1, int(round(duration_seconds * fps)))
    validated_frame_count = _validated_metadata_frame_count(video, format_data, fps)

    timecode = _tag_value(video.get("tags"), "timecode")
    if not timecode:
        timecode = _tag_value(format_data.get("tags"), "timecode")
    if not timecode:
        timecode = next(
            (
                value
                for stream in streams
                if (value := _tag_value(stream.get("tags"), "timecode"))
            ),
            None,
        )
    pixel_format = str(video.get("pix_fmt") or base.pixel_format)
    bit_rate = base.bit_rate
    for candidate in (video.get("bit_rate"), format_data.get("bit_rate")):
        try:
            candidate_rate = int(candidate)
        except (TypeError, ValueError):
            continue
        if candidate_rate > 0:
            bit_rate = candidate_rate
            break
    return (
        replace(
            base,
            codec=str(video.get("codec_name") or base.codec),
            pixel_format=pixel_format,
            width=int(video.get("width") or base.width),
            height=int(video.get("height") or base.height),
            fps=fps,
            bit_depth=pixel_format_bit_depth(pixel_format),
            duration_seconds=duration_seconds,
            frame_count=frame_count,
            timecode=timecode or base.timecode,
            bit_rate=bit_rate,
        ),
        validated_frame_count,
    )


def _count_video_frames(path: str | Path) -> int:
    process = subprocess.run(
        [
            ffmpeg_executable(),
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(path),
            "-map",
            "0:v:0",
            "-an",
            "-sn",
            "-f",
            "null",
            "-",
            "-progress",
            "pipe:1",
            "-nostats",
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    output = process.stdout.decode("utf-8", errors="replace")
    counts = [int(value) for value in re.findall(r"(?m)^frame=(\d+)\s*$", output)]
    if process.returncode or not counts or counts[-1] < 1:
        detail = process.stderr.decode("utf-8", errors="replace").strip()
        raise OSError(f"unable to count video frames: {path}: {detail}")
    return counts[-1]


def probe_video(path: str | Path, *, count_frames: bool = False) -> VideoProbe:
    try:
        process = subprocess.run(
            [ffmpeg_executable(), "-hide_banner", "-i", str(path)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            check=False,
            timeout=PROBE_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as error:
        raise PermissionError(
            f"timed out opening {path}; on macOS, allow VP Stitch access to "
            "the source folder in Privacy & Security > Files and Folders"
        ) from error
    base = parse_probe_output(path, process.stderr.decode("utf-8", errors="replace"))
    result, metadata_frame_count = _enhance_probe_with_ffprobe(path, base)
    if count_frames and metadata_frame_count is not None:
        result = replace(result, frame_count=metadata_frame_count)
    elif count_frames:
        result = replace(result, frame_count=_count_video_frames(path))
    return result


def _read_exact(stream, buffer: bytearray) -> bool:
    view = memoryview(buffer)
    offset = 0
    while offset < len(buffer):
        count = stream.readinto(view[offset:])
        if not count:
            if offset == 0:
                return False
            raise OSError(f"truncated raw frame: {offset} of {len(buffer)} bytes")
        offset += count
    return True


class VideoDecoder:
    def __init__(
        self,
        path: str | Path,
        camera: Camera,
        fps: float,
        start_time: float | None = None,
        start_frame: int = 0,
        source_fps: float | None = None,
        exact_frame_seek: bool = False,
        output_size: tuple[int, int] | None = None,
        decoder_threads: int = 2,
        source_bit_depth: int | None = None,
    ):
        self.camera = camera
        self.width, self.height = output_size or (camera.width, camera.height)
        if self.width < 1 or self.height < 1:
            raise ValueError("decoder output size must be positive")
        if decoder_threads < 1:
            raise ValueError("decoder_threads must be positive")
        self._decode_low_bit_depth = (
            source_bit_depth is not None and source_bit_depth <= 8
        )
        bytes_per_channel = 1 if self._decode_low_bit_depth else 2
        self.frame_bytes = self.width * self.height * 3 * bytes_per_channel
        self._buffer = bytearray(self.frame_bytes)
        self._expanded_buffer = (
            np.empty((self.height, self.width, 3), dtype=np.uint16)
            if self._decode_low_bit_depth
            else None
        )
        if start_frame < 0:
            raise ValueError("decoder start_frame cannot be negative")
        if start_time is not None and start_time < 0.0:
            raise ValueError("decoder start_time cannot be negative")
        seek_fps = source_fps or fps
        frame_seek = start_frame + max(camera.frame_offset, 0)
        if exact_frame_seek and start_time is not None:
            frame_seek += int(round(start_time * seek_fps))
        seek_time = (start_time or 0.0) + frame_seek / seek_fps
        command = [
            ffmpeg_executable(),
            "-hide_banner",
            "-loglevel",
            "error",
            # Five simultaneous 6K decoders using FFmpeg's automatic thread
            # count can retain several gigabytes of reference frames. A small
            # per-camera cap keeps renderer memory bounded while cameras still
            # decode concurrently.
            "-threads",
            str(decoder_threads),
            # libswscale otherwise creates a CPU-sized filter pool in every
            # camera process. At 6K RGB48 each worker can retain a full frame,
            # multiplying RSS by several gigabytes across five cameras.
            "-filter_threads",
            "1",
        ]
        if seek_time > 0.0 and not exact_frame_seek:
            # Input-side accurate seeking jumps to the nearest keyframe and then
            # decodes/discards up to the exact requested timestamp. It avoids
            # reading every leading frame for long aligned plates.
            command.extend(["-ss", f"{seek_time:.9f}", "-accurate_seek"])
        if (
            sys.platform == "darwin"
            and os.environ.get("VPSTITCH_DISABLE_VIDEOTOOLBOX_DECODE") != "1"
        ):
            # FFmpeg transparently falls back to software when a source exceeds
            # the Apple decoder's limits, while supported H.264/HEVC/ProRes
            # plates use the Mac media engine.
            command.extend(["-hwaccel", "videotoolbox"])
        command.extend(["-i", str(path)])
        command.extend(["-an", "-sn"])
        filters: list[str] = []
        if exact_frame_seek and frame_seek > 0:
            filters.extend([f"trim=start_frame={frame_seek}", "setpts=PTS-STARTPTS"])
        if source_fps is None or abs(source_fps - fps) > 0.001:
            filters.append(f"fps={fps}")
        if (
            output_size is not None
            or camera.input_color_space is not None
            or camera.input_video_range is not None
        ):
            matrix = {
                "bt709": "bt709",
                "bt2020nc": "bt2020",
                "smpte170m": "smpte170m",
            }.get(camera.input_color_space)
            scale = f"scale={self.width}:{self.height}:flags=lanczos"
            if matrix:
                scale += f":in_color_matrix={matrix}"
            if camera.input_video_range:
                scale += f":in_range={camera.input_video_range}"
            if matrix or camera.input_video_range:
                scale += ":out_range=full"
            filters.append(scale)
        if filters:
            command.extend(["-vf", ",".join(filters)])
        command.extend(
            [
                "-f",
                "rawvideo",
                "-pix_fmt",
                "rgb24" if self._decode_low_bit_depth else "rgb48le",
                "pipe:1",
            ]
        )
        self.process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=8 * 1024 * 1024,
        )
        assert self.process.stderr is not None
        self._stderr = _BoundedPipeReader(self.process.stderr)
        self._closed = False

    def read(self, *, copy: bool = True) -> np.ndarray | None:
        assert self.process.stdout is not None
        if not _read_exact(self.process.stdout, self._buffer):
            return None
        if self._decode_low_bit_depth:
            frame8 = np.frombuffer(self._buffer, dtype=np.uint8).reshape(
                self.height, self.width, 3
            )
            assert self._expanded_buffer is not None
            np.multiply(
                frame8,
                257,
                out=self._expanded_buffer,
                dtype=np.uint16,
                casting="unsafe",
            )
            return self._expanded_buffer.copy() if copy else self._expanded_buffer
        frame = np.frombuffer(self._buffer, dtype="<u2").reshape(
            self.height, self.width, 3
        )
        return frame.copy() if copy else frame

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        # A prefetch worker may currently be blocked in stdout.readinto().
        # Stop FFmpeg first so that read returns EOF; closing the buffered pipe
        # first can deadlock on its internal lock while that read is active.
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=3)
        if self.process.stdout:
            try:
                self.process.stdout.close()
            except OSError:
                pass
        stderr_reader = getattr(self, "_stderr", None)
        if stderr_reader is not None:
            stderr_reader.finish()
        if self.process.stderr:
            try:
                self.process.stderr.close()
            except OSError:
                pass


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
        encoder_threads: int = 2,
    ):
        if encoder_threads < 1:
            raise ValueError("encoder_threads must be positive")
        if video.output_codec == "hevc-444-10" and width * height > 35_651_584:
            raise ValueError(
                "hevc-444-10 output exceeds the largest standard HEVC picture level; "
                "use ffv1-16, exr-half-sequence, or ProRes"
            )
        command = [
            ffmpeg_executable(),
            "-hide_banner",
            "-loglevel",
            "warning",
            "-filter_threads",
            "1",
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
            if sys.platform == "darwin":
                command.extend(
                    [
                        "-vf",
                        _filter_chain(video),
                        "-c:v",
                        "prores_videotoolbox",
                        "-profile:v",
                        "4",
                        "-allow_sw",
                        "1",
                        *( ["-require_sw", "1"] if max(width, height) >= 16384 else [] ),
                        "-prio_speed",
                        "1",
                        "-pix_fmt",
                        "p410le",
                    ]
                )
            else:
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
            if sys.platform == "darwin":
                command.extend(
                    [
                        "-vf",
                        _filter_chain(video),
                        "-c:v",
                        "prores_videotoolbox",
                        "-profile:v",
                        "3",
                        "-allow_sw",
                        "1",
                        *( ["-require_sw", "1"] if max(width, height) >= 16384 else [] ),
                        "-prio_speed",
                        "1",
                        "-pix_fmt",
                        "p210le",
                    ]
                )
            else:
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
        elif video.output_codec == "h264-proxy":
            command.extend(
                [
                    "-vf",
                    _filter_chain(video),
                    "-c:v",
                    "libx264",
                    "-preset",
                    "veryfast",
                    "-crf",
                    "22",
                    "-pix_fmt",
                    "yuv420p",
                    "-movflags",
                    "+faststart",
                ]
            )
        else:
            raise ValueError(f"unsupported output codec: {video.output_codec}")
        command.extend(["-threads", str(encoder_threads)])
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
        assert self.process.stderr is not None
        self._stderr = _BoundedPipeReader(self.process.stderr)

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
        return_code = self.process.wait()
        stderr = self._stderr.finish()
        if self.process.stderr:
            self.process.stderr.close()
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
        assert self.process.stderr is not None
        self._stderr = _BoundedPipeReader(self.process.stderr)

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
        return_code = self.process.wait()
        stderr = self._stderr.finish()
        if self.process.stderr:
            self.process.stderr.close()
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
