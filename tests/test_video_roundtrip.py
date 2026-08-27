from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
import io
import json
import subprocess

import numpy as np
import OpenEXR
import pytest

import vpstitch.ffmpegio as ffmpegio
from vpstitch.config import Camera, Color, Lens, Video
from vpstitch.ffmpegio import DpxSequenceEncoder, VideoDecoder, VideoEncoder, probe_video
from vpstitch.imageio import ExrSequenceEncoder, read_image, write_png


class _ClosableStream:
    def __init__(self) -> None:
        self.close_count = 0

    def close(self) -> None:
        self.close_count += 1


class _HungDecoderProcess:
    def __init__(self) -> None:
        self.stdout = _ClosableStream()
        self.stderr = _ClosableStream()
        self.terminate_count = 0
        self.kill_count = 0
        self.wait_count = 0

    def poll(self) -> None:
        return None

    def terminate(self) -> None:
        self.terminate_count += 1

    def kill(self) -> None:
        self.kill_count += 1

    def wait(self, timeout: float) -> int:
        self.wait_count += 1
        if self.wait_count == 1:
            raise subprocess.TimeoutExpired("ffmpeg", timeout)
        return -9


def _camera(width: int, height: int) -> Camera:
    return Camera(
        "test", width, height, 0, 0, 0, Lens("pinhole", 100, 100, width / 2, height / 2)
    )


@pytest.mark.parametrize(
    ("codec", "suffix", "minimum_levels", "maximum_error"),
    [
        ("ffv1-16", ".mkv", 2000, 0),
        ("prores-hq", ".mov", 800, 512),
        ("prores-4444", ".mov", 1800, 512),
    ],
)
def test_high_bit_depth_video_roundtrip(
    tmp_path: Path,
    codec: str,
    suffix: str,
    minimum_levels: int,
    maximum_error: int,
) -> None:
    width, height = 2048, 32
    ramp = np.linspace(0, 65535, width, dtype=np.uint16)
    frame = np.repeat(ramp[None, :, None], height, axis=0)
    frame = np.repeat(frame, 3, axis=2)
    path = tmp_path / f"roundtrip{suffix}"
    video = Video(
        fps=24,
        frames=1,
        output_codec=codec,
        color_primaries="bt709",
        color_trc="bt709",
        colorspace="bt709",
        color_range="tv",
    )
    encoder = VideoEncoder(path, width, height, video)
    encoder.write(frame)
    encoder.close()
    decoder = VideoDecoder(path, _camera(width, height), 24)
    decoded = decoder.read()
    decoder.close()
    assert decoded is not None
    assert decoded.dtype == np.uint16
    assert np.unique(decoded[..., 0]).size >= minimum_levels
    error = np.abs(decoded.astype(np.int32) - frame.astype(np.int32))
    assert int(error.max()) <= maximum_error
    if codec.startswith("prores"):
        probe = probe_video(path)
        assert probe.bit_depth == (12 if codec == "prores-4444" else 10)
        assert probe.colorspace == "bt709"


def test_exr_sequence_preserves_half_float_extended_range(tmp_path: Path) -> None:
    width, height = 11, 7
    frame = np.linspace(-0.5, 4.0, width * height * 3, dtype=np.float16).reshape(
        height, width, 3
    )
    video = Video(fps=24, frames=1, output_codec="exr-half-sequence")
    encoder = ExrSequenceEncoder(tmp_path / "exr", width, height, video, Color())
    encoder.write(frame)
    encoder.close()
    decoded = OpenEXR.File(str(tmp_path / "exr" / "frame_000000.exr"))
    pixels = decoded.channels()["RGB"].pixels
    np.testing.assert_array_equal(pixels, frame)
    assert float(pixels.min()) < 0.0
    assert float(pixels.max()) > 1.0


def test_png_reference_roundtrip_supports_unicode_paths(tmp_path: Path) -> None:
    frame = np.random.default_rng(8).integers(
        0, 65536, (19, 31, 3), dtype=np.uint16
    )
    path = tmp_path / "기준 프레임" / "카메라 01.png"
    write_png(path, frame)
    np.testing.assert_array_equal(read_image(path), frame)


def test_decoder_start_time_selects_later_frame(tmp_path: Path) -> None:
    width, height = 64, 32
    path = tmp_path / "seek.mkv"
    video = Video(fps=1, frames=3, output_codec="ffv1-16")
    encoder = VideoEncoder(path, width, height, video)
    for value in (5000, 25000, 55000):
        encoder.write(np.full((height, width, 3), value, dtype=np.uint16))
    encoder.close()
    decoder = VideoDecoder(path, _camera(width, height), 1, start_time=1.0)
    selected = decoder.read()
    decoder.close()
    assert selected is not None
    assert int(np.median(selected)) == 25000


def test_decoder_start_frame_selects_exact_frame(tmp_path: Path) -> None:
    width, height = 64, 32
    path = tmp_path / "seek-frame.mkv"
    video = Video(fps=2, frames=3, output_codec="ffv1-16")
    encoder = VideoEncoder(path, width, height, video)
    for value in (5000, 25000, 55000):
        encoder.write(np.full((height, width, 3), value, dtype=np.uint16))
    encoder.close()
    assert probe_video(path, count_frames=True).frame_count == 3
    decoder = VideoDecoder(
        path,
        _camera(width, height),
        2,
        start_frame=2,
        source_fps=2,
        exact_frame_seek=True,
    )
    selected = decoder.read()
    decoder.close()
    assert selected is not None
    assert int(np.median(selected)) == 55000


def test_decoder_downscales_reference_frame_before_python_allocation(tmp_path: Path) -> None:
    width, height = 128, 64
    path = tmp_path / "preview-scale.mkv"
    video = Video(fps=24, frames=1, output_codec="ffv1-16")
    encoder = VideoEncoder(path, width, height, video)
    encoder.write(np.full((height, width, 3), 32000, dtype=np.uint16))
    encoder.close()
    decoder = VideoDecoder(
        path,
        _camera(width, height),
        24,
        output_size=(32, 16),
    )
    scaled = decoder.read()
    decoder.close()
    assert scaled is not None
    assert scaled.shape == (16, 32, 3)
    assert scaled.nbytes == 16 * 32 * 3 * 2
    assert int(np.median(scaled)) == pytest.approx(32000, abs=16)


def test_dpx_sequence_is_12bit_rgb(tmp_path: Path) -> None:
    width, height = 257, 32
    ramp = np.linspace(0, 65535, width, dtype=np.uint16)
    frame = np.repeat(ramp[None, :, None], height, axis=0)
    frame = np.repeat(frame, 3, axis=2)
    video = Video(fps=24, frames=1, output_codec="dpx12-sequence")
    directory = tmp_path / "dpx"
    encoder = DpxSequenceEncoder(directory, width, height, video, Color())
    encoder.write(frame)
    encoder.close()
    probe = probe_video(directory / "frame_000000.dpx")
    assert probe.bit_depth == 12
    manifest = json.loads((directory / "vpstitch_manifest.json").read_text(encoding="utf-8"))
    assert manifest["format"] == "gbrp12le-dpx-sequence"
    assert manifest["frames"] == 1


def test_h264_mp4_is_10bit(tmp_path: Path) -> None:
    width, height = 320, 180
    frame = np.full((height, width, 3), 32768, dtype=np.uint16)
    path = tmp_path / "review.mp4"
    video = Video(fps=24, frames=1, output_codec="h264-mp4-10")
    encoder = VideoEncoder(path, width, height, video)
    encoder.write(frame)
    encoder.close()
    probe = probe_video(path)
    assert probe.codec == "h264"
    assert probe.bit_depth == 10
    assert probe.bit_rate is not None and probe.bit_rate > 0


def test_decoder_applies_explicit_video_range_interpretation(tmp_path: Path) -> None:
    width, height = 320, 180
    frame = np.full((height, width, 3), 18000, dtype=np.uint16)
    path = tmp_path / "limited-range.mp4"
    video = Video(
        fps=24,
        frames=1,
        output_codec="h264-mp4-10",
        color_primaries="bt709",
        color_trc="bt709",
        colorspace="bt709",
        color_range="tv",
    )
    encoder = VideoEncoder(path, width, height, video)
    encoder.write(frame)
    encoder.close()

    auto_decoder = VideoDecoder(path, _camera(width, height), 24)
    auto = auto_decoder.read()
    auto_decoder.close()
    full_decoder = VideoDecoder(
        path,
        replace(
            _camera(width, height),
            input_color_space="bt709",
            input_video_range="pc",
        ),
        24,
    )
    full = full_decoder.read()
    full_decoder.close()
    assert auto is not None and full is not None
    assert abs(int(np.median(auto)) - int(np.median(full))) > 500


def test_decoder_close_kills_hung_ffmpeg_and_is_idempotent() -> None:
    process = _HungDecoderProcess()
    decoder = VideoDecoder.__new__(VideoDecoder)
    decoder.process = process
    decoder._closed = False

    decoder.close()
    decoder.close()

    assert process.terminate_count == 1
    assert process.kill_count == 1
    assert process.wait_count == 2
    assert process.stdout.close_count == 1
    assert process.stderr.close_count == 1


def test_decoder_caps_ffmpeg_threads_before_input(monkeypatch: pytest.MonkeyPatch) -> None:
    commands: list[list[str]] = []
    process = _HungDecoderProcess()

    def fake_popen(command: list[str], **_kwargs: object) -> _HungDecoderProcess:
        commands.append(command)
        return process

    monkeypatch.setattr(ffmpegio, "ffmpeg_executable", lambda: "ffmpeg")
    monkeypatch.setattr(ffmpegio.subprocess, "Popen", fake_popen)

    decoder = VideoDecoder("plate.mov", _camera(64, 32), 24)

    assert len(commands) == 1
    command = commands[0]
    thread_index = command.index("-threads")
    assert command[thread_index + 1] == "2"
    assert thread_index < command.index("-i")
    filter_thread_index = command.index("-filter_threads")
    assert command[filter_thread_index + 1] == "1"
    assert filter_thread_index < command.index("-i")
    decoder.close()


def test_decoder_rejects_non_positive_thread_count() -> None:
    with pytest.raises(ValueError, match="decoder_threads"):
        VideoDecoder("plate.mov", _camera(64, 32), 24, decoder_threads=0)


def test_decoder_uses_rgb24_for_known_8bit_sources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commands: list[list[str]] = []
    process = _HungDecoderProcess()

    def fake_popen(command: list[str], **_kwargs: object) -> _HungDecoderProcess:
        commands.append(command)
        return process

    monkeypatch.setattr(ffmpegio, "ffmpeg_executable", lambda: "ffmpeg")
    monkeypatch.setattr(ffmpegio.subprocess, "Popen", fake_popen)

    decoder = VideoDecoder(
        "plate.mov",
        _camera(64, 32),
        24,
        source_bit_depth=8,
    )

    command = commands[0]
    pixel_format_index = command.index("-pix_fmt")
    assert command[pixel_format_index + 1] == "rgb24"
    assert decoder.frame_bytes == 64 * 32 * 3
    assert decoder._expanded_buffer is not None
    decoder.close()


def test_decoder_expands_rgb24_into_reusable_uint16_buffer() -> None:
    decoder = VideoDecoder.__new__(VideoDecoder)
    decoder.width = 2
    decoder.height = 1
    decoder.frame_bytes = 6
    decoder._buffer = bytearray(6)
    decoder._decode_low_bit_depth = True
    decoder._expanded_buffer = np.empty((1, 2, 3), dtype=np.uint16)
    decoder.process = SimpleNamespace(
        stdout=io.BytesIO(bytes([0, 1, 2, 127, 128, 255]))
    )

    frame = decoder.read(copy=False)

    assert frame is decoder._expanded_buffer
    np.testing.assert_array_equal(
        frame,
        np.array([[[0, 257, 514], [32639, 32896, 65535]]], dtype=np.uint16),
    )


def test_encoder_caps_ffmpeg_threads(monkeypatch: pytest.MonkeyPatch) -> None:
    commands: list[list[str]] = []
    process = SimpleNamespace(stdin=_ClosableStream(), stderr=io.BytesIO(), wait=lambda: 0)

    def fake_popen(command: list[str], **_kwargs: object) -> object:
        commands.append(command)
        return process

    monkeypatch.setattr(ffmpegio, "ffmpeg_executable", lambda: "ffmpeg")
    monkeypatch.setattr(ffmpegio.subprocess, "Popen", fake_popen)

    encoder = VideoEncoder(
        "output.mov",
        64,
        32,
        Video(fps=24, frames=1, output_codec="prores-hq"),
    )

    command = commands[0]
    assert command[command.index("-filter_threads") + 1] == "1"
    assert command[command.index("-threads") + 1] == "2"
    assert command.index("-filter_threads") < command.index("-i")
    assert command.index("-threads") > command.index("-i")
    encoder.close()
