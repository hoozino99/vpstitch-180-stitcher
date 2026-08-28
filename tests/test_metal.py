from __future__ import annotations

from pathlib import Path
import sys

import numpy as np
import pytest

from vpstitch.color import (
    BUNDLED_ACES_STUDIO_ID,
    ColorPipeline,
    quantize_u16,
)
from vpstitch.config import Camera, Color, Lens
from vpstitch.ffmpegio import VideoDecoder, probe_video
from vpstitch.metal import (
    METAL_INPUT_KERNEL_NAME,
    MetalBackendError,
    MetalStitchBackend,
    NativeMetalVideoEncoder,
    build_metal_input_program,
)


def _p3_pq_color(*, color_match: bool = False) -> Color:
    return Color(
        mode="ocio",
        ocio_config=BUNDLED_ACES_STUDIO_ID,
        working_space="ACEScg",
        output_mode="display_view",
        display="ST2084-P3-D65 - Display",
        view="ACES 2.0 - HDR 1000 nits (P3 D65)",
        match_enabled=color_match,
        match_strength=0.7,
        integer_dither=False,
    )


def test_metal_input_program_supports_textureless_camera_transform() -> None:
    pytest.importorskip("PyOpenColorIO")
    program = build_metal_input_program(_p3_pq_color(), "Camera Rec.709")

    assert f"kernel void {METAL_INPUT_KERNEL_NAME}" in program.source
    assert "vp_input_ocio_ocio_input_transform ocio;" in program.source
    assert "ocio();" not in program.source


@pytest.mark.skipif(sys.platform != "darwin", reason="Metal requires macOS")
def test_metal_input_color_match_matches_cpu_pipeline() -> None:
    pytest.importorskip("PyOpenColorIO")
    library = (
        Path(__file__).resolve().parents[1]
        / ".build"
        / "macos"
        / "libvpstitch_metal.dylib"
    )
    if not library.is_file():
        pytest.skip("Metal test library is not built")

    settings = _p3_pq_color(color_match=True)
    gain = (1.02, 0.98, 1.01)
    source = np.random.default_rng(7).integers(
        0, 65536, (32, 48, 3), dtype=np.uint16
    )
    backend = MetalStitchBackend(settings, ["Camera Rec.709"], [gain])
    backend.transform_sources([source])
    map_y, map_x = np.mgrid[:32, :48].astype(np.float32)
    backend.prepare_tile(
        1,
        [0],
        [(map_x, map_y)],
        [np.ones((32, 48), dtype=np.float32)],
    )
    actual = backend.render_prepared_tile(
        1,
        48,
        32,
        tile_x=0,
        tile_y=0,
        frame_index=0,
        dither=False,
        seed=1,
    )
    batched = np.full((40, 64, 3), 65535, dtype=np.uint16)
    backend.render_prepared_frame(
        [(1, 8, 4)],
        batched,
        frame_index=0,
        dither=False,
        seed=1,
    )
    assert np.array_equal(batched[4:36, 8:56], actual)
    assert not np.any(batched[:4])
    assert not np.any(batched[:, :8])
    dithered_tile = backend.render_prepared_tile(
        1,
        48,
        32,
        tile_x=8,
        tile_y=4,
        frame_index=3,
        dither=True,
        seed=7349,
    )
    dithered_batch = np.empty((40, 64, 3), dtype=np.uint16)
    backend.render_prepared_frame(
        [(1, 8, 4)],
        dithered_batch,
        frame_index=3,
        dither=True,
        seed=7349,
    )
    assert np.array_equal(dithered_batch[4:36, 8:56], dithered_tile)

    color = ColorPipeline(settings, ["Camera Rec.709"], [gain])
    expected = quantize_u16(
        color.working_to_output(color.input_to_working(0, source)),
        False,
        1,
        0,
        0,
        0,
    )
    difference = np.abs(actual.astype(np.int32) - expected.astype(np.int32))
    assert int(difference.max()) <= 1


@pytest.mark.skipif(sys.platform != "darwin", reason="Metal requires macOS")
def test_native_prores_iosurface_roundtrip_preserves_precision_and_hdr_tags(
    tmp_path: Path,
) -> None:
    pytest.importorskip("PyOpenColorIO")
    library = (
        Path(__file__).resolve().parents[1]
        / ".build"
        / "macos"
        / "libvpstitch_metal.dylib"
    )
    if not library.is_file():
        pytest.skip("Metal test library is not built")

    width, height = 1024, 64
    x = np.linspace(0, 65535, width, dtype=np.uint16)
    y = np.linspace(0, 65535, height, dtype=np.uint16)
    source = np.empty((height, width, 3), dtype=np.uint16)
    source[..., 0] = x[None, :]
    source[..., 1] = y[:, None]
    source[..., 2] = (
        x[None, :].astype(np.uint32) + y[:, None].astype(np.uint32)
    ) // 2
    backend = MetalStitchBackend(
        _p3_pq_color(), ["Camera Rec.709"], [(1.0, 1.0, 1.0)]
    )
    backend.transform_sources([source])
    map_y, map_x = np.mgrid[:height, :width].astype(np.float32)
    backend.prepare_tile(
        1,
        [0],
        [(map_x, map_y)],
        [np.ones((height, width), dtype=np.float32)],
    )
    expected = np.empty((height, width, 3), dtype=np.uint16)
    backend.render_prepared_frame(
        [(1, 0, 0)], expected, frame_index=0, dither=False, seed=17
    )

    output = tmp_path / "native-p3-pq.mov"
    encoder = backend.create_native_prores_encoder(
        output,
        width,
        height,
        23.976,
        color_primaries="smpte432",
        color_trc="smpte2084",
        colorspace="bt2020nc",
        color_range="tv",
    )
    for frame_index in range(2):
        encoder.write_prepared(
            [(1, 0, 0)],
            frame_index=frame_index,
            dither=False,
            seed=17,
        )
    encoder.close()

    probe = probe_video(output)
    assert probe.codec == "prores"
    assert probe.bit_depth == 10
    assert probe.frame_count == 2
    assert probe.fps == pytest.approx(24000 / 1001, rel=1e-6)
    assert probe.color_primaries == "smpte432"
    assert probe.color_trc == "smpte2084"
    assert probe.colorspace == "bt2020nc"

    camera = Camera(
        "native",
        width,
        height,
        0.0,
        0.0,
        0.0,
        Lens("pinhole", 100.0, 100.0, width / 2, height / 2),
    )
    decoder = VideoDecoder(output, camera, 24000 / 1001)
    decoded = decoder.read()
    decoder.close()
    assert decoded is not None
    difference = np.abs(decoded.astype(np.int32) - expected.astype(np.int32))
    assert float(np.percentile(difference, 99.9)) <= 512
    assert int(difference.max()) <= 1024


def test_native_prores_requires_explicit_nonconstant_luminance_metadata(
    tmp_path: Path,
) -> None:
    with pytest.raises(MetalBackendError, match="explicit primaries"):
        NativeMetalVideoEncoder(
            object(),  # type: ignore[arg-type]
            tmp_path / "missing.mov",
            64,
            32,
            24.0,
            color_primaries=None,
            color_trc="bt709",
            colorspace="bt709",
            color_range="tv",
        )
    with pytest.raises(MetalBackendError, match="constant-luminance"):
        NativeMetalVideoEncoder(
            object(),  # type: ignore[arg-type]
            tmp_path / "bt2020c.mov",
            64,
            32,
            24.0,
            color_primaries="bt2020",
            color_trc="smpte2084",
            colorspace="bt2020c",
            color_range="tv",
        )
