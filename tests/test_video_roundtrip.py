from __future__ import annotations

from pathlib import Path
import json

import numpy as np
import OpenEXR
import pytest
import tifffile

from vpstitch.config import Camera, Color, Lens, Video
from vpstitch.ffmpegio import DpxSequenceEncoder, VideoDecoder, VideoEncoder, probe_video
from vpstitch.imageio import ExrSequenceEncoder, TiffSequenceEncoder


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


def test_tiff_sequence_is_exact_uint16_rgb(tmp_path: Path) -> None:
    width, height = 257, 19
    generator = np.random.default_rng(17)
    frame = generator.integers(0, 65536, (height, width, 3), dtype=np.uint16)
    video = Video(fps=23.976, frames=1, output_codec="tiff16-sequence")
    encoder = TiffSequenceEncoder(tmp_path / "sequence", width, height, video, Color())
    encoder.write(frame)
    encoder.close()
    decoded = tifffile.imread(tmp_path / "sequence" / "frame_000000.tif")
    np.testing.assert_array_equal(decoded, frame)
    manifest = json.loads(
        (tmp_path / "sequence" / "vpstitch_manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["format"] == "uint16-rgb-bigtiff-sequence"
    assert manifest["frames"] == 1


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
